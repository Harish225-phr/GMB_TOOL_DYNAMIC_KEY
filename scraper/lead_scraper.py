"""
Lead scraper orchestrator - main execution engine.
Coordinates API client, rate limiting, caching, deduplication, and website extraction.
Production-ready lead generation engine with memory optimization for free-tier.
"""

import time
import logging
import gc
from typing import Dict, List, Optional, Tuple, Generator
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from .config import config
from .models import BusinessLead, SearchResult, AggregatedSearchResult
from .api_client import CachedGooglePlacesAPIClient, GooglePlacesAPIClient
from .geo_grid import GeoGridExpander, SearchLocationManager
from .deduplicator import ResultDeduplicator
from .website_extractor import OptionalWebsiteFetcher
from .cache_manager import SmartSearchCache, CacheKey, get_global_cache
from .rate_limiter import (
    initialize_rate_limiting,
    get_rate_limiter,
    get_api_tracker
)


# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class LeadScraperEngine:
    """
    Production-grade lead scraping engine.
    Orchestrates all components for efficient lead generation.
    """
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        enable_caching: bool = True,
        enable_geo_expansion: bool = True,
        fetch_websites_by_default: bool = True,
    ):
        """
        Initialize lead scraper engine.
        
        Args:
            api_key: Google Maps API key
            enable_caching: Enable result caching
            enable_geo_expansion: Enable geo-grid expansion
            fetch_websites_by_default: Whether to fetch websites
        """
        # Initialize rate limiting
        initialize_rate_limiting(
            calls_per_second=1.0 / config.MIN_DELAY_BETWEEN_REQUESTS,
            max_concurrent=config.MAX_CONCURRENT_API_CALLS
        )
        
        # API client
        self.api_client = CachedGooglePlacesAPIClient(api_key)
        
        # Features
        self.caching_enabled = enable_caching
        self.cache = SmartSearchCache(get_global_cache()) if enable_caching else None
        self.geo_expander = GeoGridExpander(enable_geo_expansion)
        self.location_manager = SearchLocationManager(self.geo_expander)
        self.website_fetcher = OptionalWebsiteFetcher(fetch_websites_by_default)
        self.deduplicator = ResultDeduplicator()
        
        # Metrics
        self.metrics = {
            "searches_completed": 0,
            "total_api_calls": 0,
            "total_results_fetched": 0,
            "duplicates_removed": 0,
            "cache_hits": 0,
        }
    
    def _trigger_gc(self, threshold_mb: int = 100):
        """Trigger garbage collection if memory usage is high."""
        try:
            import psutil
            import os
            process = psutil.Process(os.getpid())
            memory_mb = process.memory_info().rss / 1024 / 1024
            
            if memory_mb > threshold_mb:
                logger.info(f"Memory usage high ({memory_mb:.1f}MB), triggering GC")
                gc.collect()
        except ImportError:
            # psutil not installed, just do periodic GC
            gc.collect()
        except Exception as e:
            logger.debug(f"GC check failed: {e}")
    

    def search_single_location(
        self,
        keyword: str,
        location: str,
        fetch_websites: Optional[bool] = None,
        max_pages: Optional[int] = None,
        max_results: Optional[int] = None,
        api_key: Optional[str] = None,
    ) -> SearchResult:
        """
        Search for leads in a single location.
        
        Args:
            keyword: Search keyword
            location: Search location
            fetch_websites: Override default website fetching
            max_pages: Max pages to fetch
            max_results: Max results to return
        
        Returns:
            SearchResult object
        """
        max_pages = max_pages or config.MAX_PAGES_PER_SEARCH
        max_results = max_results or config.MAX_RESULTS_PER_LOCATION
        
        logger.info(f"Searching: '{keyword}' in '{location}'")
        start_time = time.time()
        
        # Check cache first
        if self.caching_enabled:
            cached = self.cache.get_search_results(keyword, location)
            if cached:
                logger.info(f"Cache hit for '{keyword}' in '{location}'")
                self.metrics["cache_hits"] += 1
                return SearchResult(
                    keyword=keyword,
                    location=location,
                    results=[
                        BusinessLead(**r) for r in cached.get("results", [])
                    ],
                    next_page_token=cached.get("next_page_token"),
                    total_results_found=len(cached.get("results", [])),
                    total_pages_fetched=cached.get("pages_fetched", 1),
                )
        
        # Fetch from API
        results = []
        place_ids = []
        current_page_token = None
        page_count = 0
        
        while page_count < max_pages and len(results) < max_results:
            query = f"{keyword} in {location}"
            
            try:
                response, status_code = self.api_client.text_search(
                    query,
                    page_token=current_page_token,
                    api_key=api_key
                )
                
                self.metrics["total_api_calls"] += 1
                
                # Check status
                if response.get("status") not in [
                    "OK",
                    "ZERO_RESULTS"
                ]:
                    logger.warning(
                        f"API Error: {response.get('status')} - "
                        f"{response.get('error_message')}"
                    )
                    break
                
                # Extract results
                page_results = response.get("results", [])
                if not page_results:
                    break
                
                for place in page_results:
                    if len(results) >= max_results:
                        break
                    
                    result = BusinessLead(
                        name=place.get("name"),
                        rating=place.get("rating"),
                        reviews_count=place.get("user_ratings_total"),
                        place_id=place.get("place_id"),
                        location=location,
                    )
                    results.append(result)
                    place_ids.append(place.get("place_id"))
                
                # Check for pagination
                current_page_token = response.get("next_page_token")
                if not current_page_token or len(results) >= max_results:
                    break
                
                page_count += 1
                
                # Delay before next page (Google requires delay)
                if current_page_token:
                    time.sleep(1)
            
            except Exception as e:
                logger.error(f"Error fetching page {page_count}: {str(e)}")
                break
        
        # Fetch websites if enabled
        if place_ids:
            websites = self.website_fetcher.fetch_websites_if_needed(
                place_ids,
                fetch_websites,
                api_key=api_key
            )
            for result in results:
                if result.place_id in websites:
                    result.website = websites[result.place_id]
        
        self.metrics["total_results_fetched"] += len(results)
        
        # Create SearchResult
        search_result = SearchResult(
            keyword=keyword,
            location=location,
            results=results,
            next_page_token=current_page_token,
            total_results_found=len(results),
            total_pages_fetched=page_count + 1,
        )
        
        # Cache results
        if self.caching_enabled:
            cache_data = {
                "results": [r.to_dict() for r in results],
                "pages_fetched": page_count + 1,
            }
            self.cache.cache_search_results(keyword, location, cache_data)
        
        # Trigger GC if needed
        self._trigger_gc(threshold_mb=80)
        
        elapsed = time.time() - start_time
        logger.info(
            f"Search completed: '{keyword}' in '{location}' - "
            f"Found {len(results)} results in {elapsed:.2f}s"
        )
        
        return search_result
    
    def search_with_expansion(
        self,
        keyword: str,
        location: str,
        fetch_websites: Optional[bool] = None,
        max_results_per_location: Optional[int] = None,
        api_key: Optional[str] = None,
    ) -> AggregatedSearchResult:
        """
        Search across expanded locations using geo-grid.
        
        Args:
            keyword: Search keyword
            location: Primary location
            fetch_websites: Override website fetching
            max_results_per_location: Max results per location
        
        Returns:
            AggregatedSearchResult with deduplicated results
        """
        max_results_per_location = (
            max_results_per_location or config.MAX_RESULTS_PER_LOCATION
        )
        
        logger.info(f"Starting expanded search: '{keyword}' in '{location}'")
        start_time = time.time()
        
        # Get expanded locations
        locations_to_search = self.location_manager.get_search_locations(
            location,
            use_expansion=True
        )
        
        logger.info(
            f"Expansion result: {len(locations_to_search)} areas to search"
        )
        
        # Search all locations
        results_by_location: Dict[str, List[BusinessLead]] = {}
        
        for search_location in locations_to_search:
            try:
                result = self.search_single_location(
                    keyword,
                    search_location,
                    fetch_websites=fetch_websites,
                    api_key=api_key,
                )
                results_by_location[search_location] = result.results
                
            except Exception as e:
                logger.error(f"Error searching {search_location}: {str(e)}")
                results_by_location[search_location] = []
        
        # Consolidate and deduplicate results
        all_results = []
        for loc_results in results_by_location.values():
            all_results.extend(loc_results)
        
        # Deduplicate
        unique_results, dedup_stats = self.deduplicator.deduplicate(
            results_by_location
        )
        
        self.metrics["duplicates_removed"] += dedup_stats["duplicates_removed"]
        self.metrics["searches_completed"] += 1
        
        # Calculate results per location
        results_per_location = {
            loc: len(results) for loc, results in results_by_location.items()
        }
        
        # Create aggregated result
        aggregated = AggregatedSearchResult(
            keyword=keyword,
            primary_location=location,
            results=unique_results,
            results_by_location=results_per_location,
            total_unique_results=len(unique_results),
            total_results_before_dedup=len(all_results),
            dedup_count=dedup_stats["duplicates_removed"],
            expanded_locations=locations_to_search,
        )
        
        # Trigger GC after large operation
        self._trigger_gc(threshold_mb=100)
        
        elapsed = time.time() - start_time
        logger.info(
            f"Expanded search completed: {len(unique_results)} unique results "
            f"from {len(all_results)} total in {elapsed:.2f}s"
        )
        
        return aggregated
    
    def get_metrics(self) -> Dict:
        """Get performance metrics."""
        api_stats = get_api_tracker()
        
        return {
            **self.metrics,
            "api_calls_per_minute": api_stats.get_calls_per_minute(),
            "cache_stats": self.cache.get_cache_stats() if self.cache else None,
        }
    
    def clear_caches(self):
        """Clear all caches."""
        if self.cache:
            self.cache.cache.clear()
        self.website_fetcher.get_extractor().clear_cache()
        self.api_client.clear_caches()
    
    def close(self):
        """Close client connections."""
        self.api_client.close()


# Convenience function for single search
def search_leads(
    keyword: str,
    location: str,
    use_expansion: bool = True,
    fetch_websites: bool = True,
    max_results: int = 60,
) -> Dict:
    """
    Quick search function.
    
    Args:
        keyword: Search keyword
        location: Search location
        use_expansion: Use geo-grid expansion
        fetch_websites: Fetch websites
        max_results: Maximum results
    
    Returns:
        Results as dictionary
    """
    engine = LeadScraperEngine(
        enable_caching=True,
        enable_geo_expansion=use_expansion,
        fetch_websites_by_default=fetch_websites,
    )
    
    try:
        if use_expansion:
            result = engine.search_with_expansion(
                keyword,
                location,
                fetch_websites
            )
        else:
            result = engine.search_single_location(
                keyword,
                location,
                fetch_websites
            )
        
        return result.to_dict() if hasattr(result, 'to_dict') else result
    
    finally:
        engine.close()

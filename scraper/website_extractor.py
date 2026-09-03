"""
Website extraction and fetching from Google Places API.
Handles parallel website fetching with caching and timeouts.
"""

import time
from typing import Optional, List, Dict, Set
from concurrent.futures import ThreadPoolExecutor, as_completed, Future
from functools import lru_cache
import threading

from .config import config
from .api_client import GooglePlacesAPIClient
from .rate_limiter import get_concurrency_controller


class WebsiteExtractor:
    """
    Extracts website information for business leads.
    Uses parallel requests with caching for efficiency.
    """
    
    def __init__(self, api_client: Optional[GooglePlacesAPIClient] = None):
        """
        Initialize website extractor.
        
        Args:
            api_client: GooglePlacesAPIClient instance (creates new if None)
        """
        self.api_client = api_client or GooglePlacesAPIClient()
        self._website_cache: Dict[str, Optional[str]] = {}
        self._cache_lock = threading.Lock()
    
    def extract_website(self, place_id: str, api_key: Optional[str] = None) -> Optional[str]:
        """
        Extract website for a single place.
        
        Args:
            place_id: Google Place ID
            api_key: Optional Google API key
        
        Returns:
            Website URL or None
        """
        if not place_id:
            return None
        
        # Check cache first
        with self._cache_lock:
            if place_id in self._website_cache:
                return self._website_cache[place_id]
        
        try:
            # Use concurrency controller for safe parallel requests
            with get_concurrency_controller():
                response, status_code = self.api_client.get_place_details(
                    place_id,
                    fields=["website"],
                    api_key=api_key
                )
            
            if response.get("status") == GooglePlacesAPIClient.STATUS_OK:
                website = response.get("result", {}).get("website")
                
                # Cache the result
                with self._cache_lock:
                    self._website_cache[place_id] = website
                
                return website
            
            # Cache None for not found
            with self._cache_lock:
                self._website_cache[place_id] = None
            
            return None
            
        except Exception as e:
            print(f"[WARNING] Error fetching website for {place_id}: {str(e)}")
            # Cache failure
            with self._cache_lock:
                self._website_cache[place_id] = None
            return None
    
    def extract_websites_batch(
        self,
        place_ids: List[str],
        skip_none: bool = False,
        api_key: Optional[str] = None
    ) -> Dict[str, Optional[str]]:
        """
        Extract websites for multiple places in parallel.
        
        Args:
            place_ids: List of Google Place IDs
            skip_none: If True, don't return None values
            api_key: Optional Google API key
        
        Returns:
            Dict mapping place_id to website URL
        """
        results = {}
        
        if not place_ids:
            return results
        
        # Filter out already cached
        to_fetch = []
        for pid in place_ids:
            with self._cache_lock:
                if pid in self._website_cache:
                    website = self._website_cache[pid]
                    if website or not skip_none:
                        results[pid] = website
                else:
                    to_fetch.append(pid)
        
        if not to_fetch:
            return results
        
        # Fetch uncached results in parallel
        with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as executor:
            futures: Dict[Future, str] = {
                executor.submit(self.extract_website, pid, api_key): pid
                for pid in to_fetch
            }
            
            try:
                for future in as_completed(
                    futures,
                    timeout=config.WEBSITE_FETCH_TIMEOUT
                ):
                    place_id = futures[future]
                    try:
                        website = future.result()
                        if website or not skip_none:
                            results[place_id] = website
                    except Exception as e:
                        print(
                            f"[WARNING] Future error for {place_id}: {str(e)}"
                        )
                        results[place_id] = None
            
            except Exception as e:
                print(f"[WARNING] Website batch fetch timeout: {str(e)}")
        
        return results
    
    def clear_cache(self):
        """Clear website cache."""
        with self._cache_lock:
            self._website_cache.clear()
    
    def get_cache_info(self) -> Dict:
        """Get cache statistics."""
        with self._cache_lock:
            total = len(self._website_cache)
            websites = sum(
                1 for v in self._website_cache.values()
                if v is not None
            )
        
        return {
            "total_cached": total,
            "websites_cached": websites,
            "none_cached": total - websites,
        }


class RobustWebsiteExtractor:
    """
    Enhanced website extractor with fallback strategies.
    """
    
    def __init__(self):
        self.extractor = WebsiteExtractor()
    
    def extract_with_fallback(
        self,
        place_id: str,
        place_details: Optional[Dict] = None
    ) -> Optional[str]:
        """
        Extract website with fallback strategies.
        
        Args:
            place_id: Google Place ID
            place_details: Pre-fetched place details dict
        
        Returns:
            Website URL or None
        """
        # Try provided details first (if already fetched)
        if place_details and "website" in place_details:
            website = place_details.get("website")
            if website:
                return website
        
        # Fetch from API
        website = self.extractor.extract_website(place_id)
        if website:
            return website
        
        # Could add fallback strategies here:
        # - Search business name in additional APIs
        # - Use cached snapshot from previous search
        # etc.
        
        return None


class OptionalWebsiteFetcher:
    """
    Provides control over whether to fetch websites or not.
    Useful for fast searches that don't need website info.
    """
    
    def __init__(self, enabled_by_default: bool = True):
        """
        Initialize optional website fetcher.
        
        Args:
            enabled_by_default: Whether to fetch websites by default
        """
        self.enabled_by_default = enabled_by_default
        self.extractor = WebsiteExtractor()
    
    def fetch_websites_if_needed(
        self,
        place_ids: List[str],
        fetch_enabled: Optional[bool] = None,
        api_key: Optional[str] = None
    ) -> Dict[str, Optional[str]]:
        """
        Conditionally fetch websites.
        
        Args:
            place_ids: List of place IDs
            fetch_enabled: Override default setting (None = use default)
            api_key: Optional Google API key
        
        Returns:
            Dict of place_id -> website (empty dict if fetching disabled)
        """
        should_fetch = (
            fetch_enabled if fetch_enabled is not None
            else self.enabled_by_default
        )
        
        if not should_fetch:
            print("[INFO] Website fetching disabled - skipping website extraction")
            return {}
        
        return self.extractor.extract_websites_batch(place_ids, api_key=api_key)
    
    def get_extractor(self) -> WebsiteExtractor:
        """Get underlying extractor for direct access."""
        return self.extractor


# LRU cache for single lookups (thread-safe limit cache)
@lru_cache(maxsize=1000)
def cached_get_website(place_id: str, api_key: str) -> Optional[str]:
    """
    LRU cached website lookup.
    Note: api_key included in cache key to ensure security.
    
    Args:
        place_id: Google Place ID
        api_key: API key (for cache isolation)
    
    Returns:
        Website URL or None
    """
    try:
        client = GooglePlacesAPIClient(api_key)
        response, _ = client.get_place_details(
            place_id,
            fields=["website"]
        )
        return response.get("result", {}).get("website")
    except Exception:
        return None

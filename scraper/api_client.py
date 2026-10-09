"""
Low-level Google Places API wrapper.
Handles HTTP requests with error handling, retries, and rate limiting.
Enhanced with connection pooling, adaptive backoff, and quota management.
"""

import requests
import time
import logging
from typing import Dict, Any, Optional, Tuple
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib3.poolmanager import PoolManager

from .config import config
from .rate_limiter import get_rate_limiter, get_api_tracker

logger = logging.getLogger(__name__)


class GooglePlacesAPIClient:
    """
    Wrapper for Google Places API endpoints.
    Provides methods for text search and place details with built-in error handling.
    Includes connection pooling, adaptive rate limiting, and quota management.
    """
    
    # API endpoints
    TEXT_SEARCH_ENDPOINT = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    PLACE_DETAILS_ENDPOINT = "https://maps.googleapis.com/maps/api/place/details/json"
    
    # Status codes
    STATUS_OK = "OK"
    STATUS_ZERO_RESULTS = "ZERO_RESULTS"
    STATUS_OVER_QUERY_LIMIT = "OVER_QUERY_LIMIT"
    STATUS_REQUEST_DENIED = "REQUEST_DENIED"
    STATUS_INVALID_REQUEST = "INVALID_REQUEST"
    STATUS_UNKNOWN_ERROR = "UNKNOWN_ERROR"
    STATUS_NOT_FOUND = "NOT_FOUND"
    
    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize API client.
        
        Args:
            api_key: Google Maps API key (uses config default if None)
        """
        self.api_key = api_key or config.GOOGLE_API_KEY
        
        # Create session with optimized connection pooling

        self.session = self._create_session()
        
        # Track consecutive quota errors for adaptive backoff
        self._quota_backoff_multiplier = 1.0
        self._last_quota_error_time = 0
    
    @staticmethod
    def _create_session() -> requests.Session:
        """Create requests session with optimized retry strategy and connection pooling."""
        session = requests.Session()
        
        # Configure retry strategy for network errors
        # Uses exponential backoff with multipliers
        retry_strategy = Retry(
            total=5,  # More retries for stability
            connect=3,
            read=2,
            backoff_factor=0.5,  # Starts at 0.5s: 0.5, 1.0, 2.0, 4.0, 8.0
            status_forcelist=[408, 429, 500, 502, 503, 504],  # Include 408 timeout
            allowed_methods=["GET"],
            raise_on_status=False  # Don't raise, let us handle status
        )
        
        # Create adapter with connection pooling
        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=10,  # Number of connection pools
            pool_maxsize=10,       # Max connections per pool
            pool_block=False       # Non-blocking when pool exhausted
        )
        
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        
        # Set useful headers for keep-alive and compression
        session.headers.update({
            "Connection": "keep-alive",
            "Accept-Encoding": "gzip, deflate",
            "User-Agent": "GMD-Tool/2.0"
        })
        
        return session
    
    def _handle_quota_error(self):
        """Handle quota error with adaptive backoff."""
        current_time = time.time()
        time_since_last_error = current_time - self._last_quota_error_time
        
        # Reset multiplier if enough time has passed
        if time_since_last_error > 300:  # 5 minutes
            self._quota_backoff_multiplier = 1.0
        else:
            # Exponential backoff: 2x each time
            self._quota_backoff_multiplier = min(self._quota_backoff_multiplier * 2, 32)
        
        self._last_quota_error_time = current_time
        
        backoff_seconds = 2 * self._quota_backoff_multiplier
        logger.warning(
            f"Quota limit hit. Backing off for {backoff_seconds:.1f}s. "
            f"Multiplier: {self._quota_backoff_multiplier:.1f}x"
        )
        
        return backoff_seconds
    
    def _playwright_search(self, query: str, limit: int = 20) -> Tuple[Dict[str, Any], int]:
        """Fallback to scraping Google Maps using Playwright when no API key is provided."""
        logger.info(f"Using Playwright free fallback for query: {query}")
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                
                url = f"https://www.google.com/maps/search/{query.replace(' ', '+')}"
                page.goto(url)
                
                try:
                    feed = page.wait_for_selector('div[role="feed"]', timeout=10000)
                    # Scroll feed to load more results
                    logger.info(f"Scrolling feed to load more results for {query}...")
                    for _ in range(5):
                        feed.evaluate("element => element.scrollTop = element.scrollHeight")
                        import time
                        time.sleep(1.5)
                except Exception as e:
                    logger.warning(f"Timeout waiting for feed in playwright: {e}")
                
                import time
                time.sleep(2)
                
                results = []
                elements = page.query_selector_all('a[href*="/maps/place/"]')
                logger.info(f"Playwright found {len(elements)} elements, extracting details...")
                
                # Try to fetch up to limit (max 30 to prevent complete timeout)
                fetch_limit = min(limit, len(elements), 30) 
                
                for i in range(fetch_limit):
                    try:
                        el = elements[i]
                        label = el.get_attribute("aria-label") or ""
                        href = el.get_attribute("href") or ""
                        
                        if not href or "/maps/place/" not in href:
                            continue
                            
                        # Parse basic info from label (e.g. "Name, 4.5 stars, 100 reviews...")
                        place_id = href.split('/maps/place/')[-1].split('/')[0]
                        rating = 4.5
                        reviews = 50
                        
                        import re
                        rating_match = re.search(r'([\d\.]+)\s+stars', label)
                        if rating_match:
                            try: rating = float(rating_match.group(1))
                            except: pass
                            
                        review_match = re.search(r'([\d\,]+)\s+reviews', label)
                        if review_match:
                            try: reviews = int(review_match.group(1).replace(',', ''))
                            except: pass
                        
                        # Click to get deep details (website, phone, address)
                        el.click()
                        page.wait_for_selector('h1', timeout=5000)
                        
                        phone_el = page.query_selector('button[data-item-id^="phone:tel:"]')
                        phone = phone_el.get_attribute("aria-label") if phone_el else ""
                        if phone and "Phone number: " in phone:
                            phone = phone.replace("Phone number: ", "").strip()
                            
                        web_el = page.query_selector('a[data-item-id="authority"]')
                        website = web_el.get_attribute("href") if web_el else ""
                        
                        add_el = page.query_selector('button[data-item-id="address"]')
                        address = add_el.get_attribute("aria-label") if add_el else ""
                        if address and "Address: " in address:
                            address = address.replace("Address: ", "").strip()
                        
                        results.append({
                            "name": label.split(',')[0] if label else "Unknown Business",
                            "place_id": place_id,
                            "rating": rating,
                            "user_ratings_total": reviews,
                            "formatted_address": address if address else f"{label.split(',')[0]} - {query}",
                            "website": website,
                            "formatted_phone_number": phone
                        })
                    except Exception as e:
                        logger.warning(f"Error extracting detail for item {i}: {e}")
                        continue
                
                unique_results = []
                seen = set()
                for r in results:
                    if r['name'] not in seen:
                        seen.add(r['name'])
                        unique_results.append(r)
                
                browser.close()
                return {"status": self.STATUS_OK, "results": unique_results[:limit], "next_page_token": None}, 200
        except ImportError:
            logger.error("Playwright not installed. Run 'pip install playwright' and 'playwright install'")
            return {"status": self.STATUS_UNKNOWN_ERROR, "error_message": "Playwright not installed"}, 500
        except Exception as e:
            logger.error(f"Playwright error: {str(e)}")
            return {"status": self.STATUS_UNKNOWN_ERROR, "error_message": str(e)}, 500

    def text_search(
        self,
        query: str,
        page_token: Optional[str] = None,
        region: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], int]:
        """
        Perform text search using Google Places Text Search API.
        
        Args:
            query: Search query (keyword + location)
            page_token: Token for pagination
            region: Region bias code (ISO 3166-1)
        
        Returns:
            Tuple of (response_dict, status_code)
            
        Raises:
            Exception: For critical errors
        """
        # Apply rate limiting
        get_rate_limiter().wait_if_needed()
        
        active_api_key = api_key or self.api_key
        if not active_api_key or active_api_key == "NOMINATIM_FALLBACK":
            return self._playwright_search(query)
            
        params = {
            "query": query,
            "key": active_api_key,
        }
        
        if page_token:
            params["pagetoken"] = page_token
        
        if region:
            params["region"] = region
        
        try:
            response = self.session.get(
                self.TEXT_SEARCH_ENDPOINT,
                params=params,
                timeout=config.REQUEST_TIMEOUT,
                allow_redirects=True
            )
            
            # Check response status
            if response.status_code == 200:
                data = response.json()
                
                # Check for API-level quota error
                if data.get("status") == self.STATUS_OVER_QUERY_LIMIT:
                    backoff_seconds = self._handle_quota_error()
                    time.sleep(backoff_seconds)
                    # Return the response anyway (client can handle partial results)
                
                # Record successful call
                if data.get("status") in (self.STATUS_OK, self.STATUS_ZERO_RESULTS):
                    get_api_tracker().record_call()
                
                return data, response.status_code
            
            elif response.status_code == 429:
                # Rate limit from HTTP
                backoff_seconds = self._handle_quota_error()
                time.sleep(backoff_seconds)
                return {"status": self.STATUS_OVER_QUERY_LIMIT, "results": []}, 429
            
            else:
                # Try to parse as JSON anyway
                try:
                    data = response.json()
                except:
                    data = {"error": f"HTTP {response.status_code}"}
                
                logger.warning(f"Unexpected status code {response.status_code} for query: {query}")
                return data, response.status_code
            
        except requests.exceptions.Timeout:
            raise TimeoutError(
                f"API request timed out after {config.REQUEST_TIMEOUT}s for query: {query}"
            )
        except requests.exceptions.ConnectionError as e:
            raise ConnectionError(f"Connection error: {str(e)}")
        except requests.exceptions.RequestException as e:
            logger.error(f"Request exception: {str(e)}")
            raise Exception(f"Request failed: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error in text_search: {str(e)}")
            raise Exception(f"Unexpected error in text_search: {str(e)}")

    
    def get_place_details(
        self,
        place_id: str,
        fields: Optional[list] = None,
        api_key: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], int]:
        """
        Get detailed information about a place.
        
        Args:
            place_id: Google Place ID
            fields: Specific fields to fetch (more efficient)
        
        Returns:
            Tuple of (response_dict, status_code)
        """
        # Apply rate limiting
        get_rate_limiter().wait_if_needed()
        
        if fields is None:
            fields = ["website", "name", "rating", "user_ratings_total"]
        
        active_api_key = api_key or self.api_key
        if not active_api_key or active_api_key == "NOMINATIM_FALLBACK":
            # For fallback mode, we return basic empty details as Nominatim doesn't have a separate details API based on place_id easily accessible like Google's
            return {"status": self.STATUS_OK, "result": {}}, 200
            
        params = {
            "place_id": place_id,
            "fields": ",".join(fields),
            "key": active_api_key,
        }
        
        try:
            response = self.session.get(
                self.PLACE_DETAILS_ENDPOINT,
                params=params,
                timeout=config.REQUEST_TIMEOUT,
                allow_redirects=True
            )
            
            if response.status_code == 200:
                data = response.json()
                
                # Check for API-level quota error
                if data.get("status") == self.STATUS_OVER_QUERY_LIMIT:
                    backoff_seconds = self._handle_quota_error()
                    time.sleep(backoff_seconds)
                
                # Record successful call
                if data.get("status") == self.STATUS_OK:
                    get_api_tracker().record_call()
                
                return data, response.status_code
            
            elif response.status_code == 429:
                backoff_seconds = self._handle_quota_error()
                time.sleep(backoff_seconds)
                return {"status": self.STATUS_OVER_QUERY_LIMIT}, 429
            
            else:
                try:
                    data = response.json()
                except:
                    data = {"error": f"HTTP {response.status_code}"}
                
                return data, response.status_code
                
        except requests.exceptions.Timeout:
            raise TimeoutError(f"Place details request timed out for place_id: {place_id}")
        except requests.exceptions.ConnectionError as e:
            raise ConnectionError(f"Connection error: {str(e)}")
        except Exception as e:
            logger.error(f"Error fetching place details: {str(e)}")
            raise Exception(f"Error fetching place details: {str(e)}")

    
    @staticmethod
    def is_success_status(status: str) -> bool:
        """Check if API response status is successful."""
        return status in (
            GooglePlacesAPIClient.STATUS_OK,
            GooglePlacesAPIClient.STATUS_ZERO_RESULTS,
        )
    
    @staticmethod
    def is_quota_error(status: str) -> bool:
        """Check if error is related to quota."""
        return status == GooglePlacesAPIClient.STATUS_OVER_QUERY_LIMIT
    
    def close(self):
        """Close session."""
        if self.session:
            self.session.close()
    
    def __enter__(self):
        """Context manager entry."""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()


class CachedGooglePlacesAPIClient(GooglePlacesAPIClient):
    """
    Google Places API client with built-in result caching.
    Reduces API calls for repeated searches.
    """
    
    def __init__(self, api_key: Optional[str] = None):
        """Initialize cached API client."""
        super().__init__(api_key)
        self._search_cache: Dict[str, Dict[str, Any]] = {}
        self._details_cache: Dict[str, Dict[str, Any]] = {}
    
    def text_search(
        self,
        query: str,
        page_token: Optional[str] = None,
        region: Optional[str] = None,
        use_cache: bool = True,
        api_key: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], int]:
        """
        Text search with optional caching.
        
        Args:
            query: Search query
            page_token: Pagination token
            region: Region bias
            use_cache: Whether to use cache for this search
        
        Returns:
            Tuple of (response_dict, status_code)
        """
        # Generate cache key (only cache first page)
        cache_key = f"{query}|{region}" if not page_token and use_cache else None
        
        if cache_key and cache_key in self._search_cache:
            return self._search_cache[cache_key], 200
        
        # Fetch from API
        result, status = super().text_search(query, page_token, region, api_key)
        
        # Cache first page results
        if cache_key:
            self._search_cache[cache_key] = result
        
        return result, status
    
    def get_place_details(
        self,
        place_id: str,
        fields: Optional[list] = None,
        use_cache: bool = True,
        api_key: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], int]:
        """
        Get place details with optional caching.
        
        Args:
            place_id: Place ID
            fields: Fields to fetch
            use_cache: Whether to use cache
        
        Returns:
            Tuple of (response_dict, status_code)
        """
        if use_cache and place_id in self._details_cache:
            return self._details_cache[place_id], 200
        
        result, status = super().get_place_details(place_id, fields, api_key)
        
        if use_cache:
            self._details_cache[place_id] = result
        
        return result, status
    
    def clear_caches(self):
        """Clear all caches."""
        self._search_cache.clear()
        self._details_cache.clear()

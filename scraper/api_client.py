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
            ValueError: If API key is invalid
            Exception: For critical errors
        """
        # Apply rate limiting
        get_rate_limiter().wait_if_needed()
        
        active_api_key = api_key or self.api_key
        if not active_api_key:
            raise ValueError("Google Maps API key not provided")
            
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
        if not active_api_key:
            raise ValueError("Google Maps API key not provided")
            
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

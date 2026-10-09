"""
Configuration and constants for the lead scraping system.
Centralized configuration for performance tuning and API limits.
"""

import os
from dataclasses import dataclass
from typing import Optional
from dotenv import load_dotenv

# Load environment variables
load_dotenv()


@dataclass
class AppConfig:
    """Application configuration settings."""
    
    # Google Maps API Configuration
    GOOGLE_API_KEY: str = os.getenv("GOOGLE_MAPS_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
    
    # Threading and Concurrency
    MAX_WORKERS: int = int(os.getenv("MAX_WORKERS", "5"))  # Thread pool size (increased for faster parallelism)
    MAX_CONCURRENT_API_CALLS: int = int(os.getenv("MAX_CONCURRENT_API_CALLS", "8"))  # Increased for better throughput
    REQUEST_TIMEOUT: int = int(os.getenv("REQUEST_TIMEOUT", "20"))  # seconds
    
    # Rate Limiting (requests per second per location)
    MIN_DELAY_BETWEEN_REQUESTS: float = float(os.getenv("MIN_DELAY_BETWEEN_API_CALLS", "0.2"))  # 200ms
    
    # Pagination Configuration
    MAX_PAGES_PER_SEARCH: int = int(os.getenv("MAX_PAGES_PER_SEARCH", "3"))  # Max 60 results (20 per page)
    RESULTS_PER_PAGE: int = 20  # Google Places API returns max 20 per page
    
    # Result Limits
    MAX_RESULTS_PER_LOCATION: int = int(os.getenv("MAX_RESULTS_PER_LOCATION", "60"))
    
    # Caching Configuration
    CACHE_ENABLED: bool = True
    CACHE_WEBSITE_TTL: int = 86400 * 7  # 7 days for website cache
    CACHE_SEARCH_TTL: int = 3600  # 1 hour for search results
    
    # Website Fetching
    FETCH_WEBSITES_BY_DEFAULT: bool = False  # Disabled by default to avoid timeouts - enable if needed
    WEBSITE_FETCH_TIMEOUT: int = 10  # seconds (reduced from 30)
    
    # Logging
    DEBUG_MODE: bool = os.getenv("DEBUG_MODE", "False").lower() == "true"


class GeoGridAreas:
    """Pre-configured geographic grids for major cities."""
    
    CITY_AREAS = {
        "delhi": {
            "description": "Delhi NCR",
            "areas": [
                "Central Delhi",
                "North Delhi",
                "South Delhi",
                "East Delhi",
                "West Delhi",
                "New Delhi",
                "Gurugram",
                "Noida",
                "Greater Noida",
            ]
        },
        "mumbai": {
            "description": "Greater Mumbai",
            "areas": [
                "South Mumbai",
                "Central Mumbai",
                "Zone 1 Mumbai",
                "Thane",
                "Navi Mumbai",
                "Powai",
                "Bandra",
                "Worli",
                "Andheri",
            ]
        },
        "bangalore": {
            "description": "Bangalore Metropolitan",
            "areas": [
                "Whitefield",
                "Koramangala",
                "Indiranagar",
                "Jayanagar",
                "JP Nagar",
                "HSR Layout",
                "Marathahalli",
                "Electronic City",
            ]
        },
        "hyderabad": {
            "description": "Greater Hyderabad",
            "areas": [
                "Hitech City",
                "GACHIBOWLI",
                "Kondapur",
                "Jubilee Hills",
                "Secunderabad",
                "LB Nagar",
                "Kukatpally",
            ]
        },
        "pune": {
            "description": "Pune Metropolitan",
            "areas": [
                "Hinjewadi",
                "Wakad",
                "Viman Nagar",
                "Baner",
                "Koregaon Park",
                "Hadapsar",
                "Kothrud",
            ]
        },
        "kolkata": {
            "description": "Greater Kolkata",
            "areas": [
                "Salt Lake",
                "Howrah",
                "Barrackpore",
                "Dakshineshwar",
                "Tollygunge",
                "New Town",
                "South Kolkata",
            ]
        },
        "london": {
            "description": "Greater London",
            "areas": [
                "Central London",
                "North London",
                "South London",
                "East London",
                "West London",
                "Southwest London",
                "Northwest London",
            ]
        },
        "new york": {
            "description": "NYC Metropolitan",
            "areas": [
                "Manhattan",
                "Brooklyn",
                "Queens",
                "Bronx",
                "Staten Island",
                "Long Island City",
                "Jersey City",
            ]
        },
        "austin": {
            "description": "Austin TX",
            "areas": [
                "Downtown Austin",
                "North Austin",
                "South Austin",
                "East Austin",
                "West Lake Hills",
                "Cedar Park",
                "Pflugerville",
            ]
        },
    }
    
    @classmethod
    def get_areas(cls, city: str) -> Optional[list]:
        """Get geo-grid areas for a city."""
        city_lower = city.lower().strip()
        city_data = cls.CITY_AREAS.get(city_lower)
        return city_data["areas"] if city_data else None
    
    @classmethod
    def has_geo_grid(cls, city: str) -> bool:
        """Check if city has predefined geo-grid."""
        return city.lower().strip() in cls.CITY_AREAS
    
    @classmethod
    def expand_search_locations(cls, location: str) -> list:
        """Expand a location into multiple search areas if geo-grid exists."""
        areas = cls.get_areas(location)
        if areas:
            # Return list of locations to search: original + all areas
            return [location] + areas
        # Fallback to original location only
        return [location]


# Global config instance
config = AppConfig()

# Validate configuration
if not config.GOOGLE_API_KEY:
    print("WARNING: Google API key not configured. Check environment variables. Using free fallback.")

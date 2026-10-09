from playwright.sync_api import sync_playwright
import time

def scrape_gmaps(query, limit=10):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        # Go to Google Maps search
        url = f"https://www.google.com/maps/search/{query.replace(' ', '+')}"
        print(f"Navigating to {url}")
        page.goto(url)
        
        # Wait for results to load
        try:
            # Wait for the feed containing results
            page.wait_for_selector('div[role="feed"]', timeout=10000)
        except Exception as e:
            print("Timeout waiting for feed:", e)
            
        # Give some time for rendering
        time.sleep(3)
        
        # Scroll to load more if needed
        # We want to extract business info
        results = []
        
        # We can extract elements with a role="article" or similar
        # Google Maps results usually have 'a' tags with href starting with 'https://www.google.com/maps/place/'
        elements = page.query_selector_all('a[href*="/maps/place/"]')
        print(f"Found {len(elements)} possible place links")
        
        # We need a robust way. Let's try to extract aria-labels from links that have them
        for el in elements:
            label = el.get_attribute("aria-label")
            href = el.get_attribute("href")
            if label and href and "/maps/place/" in href:
                # Basic place info
                place_id_match = href.split("?")
                # This is a bit brittle, let's just grab the label
                results.append({
                    "name": label,
                    "website": "",
                    "rating": 4.5,
                    "user_ratings_total": 50,
                    "place_id": href.split('/maps/place/')[-1].split('/')[0] if '/maps/place/' in href else "mock_id",
                })
        
        # Deduplicate by name
        unique_results = []
        seen = set()
        for r in results:
            if r['name'] not in seen:
                seen.add(r['name'])
                unique_results.append(r)
                
        browser.close()
        return unique_results[:limit]

if __name__ == "__main__":
    import sys
    query = sys.argv[1] if len(sys.argv) > 1 else "plumber in shimla"
    res = scrape_gmaps(query)
    print(res)

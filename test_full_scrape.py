from playwright.sync_api import sync_playwright
import time
import json

def scrape_gmaps_full(query):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        url = f"https://www.google.com/maps/search/{query.replace(' ', '+')}"
        page.goto(url)
        
        try:
            page.wait_for_selector('a[href*="/maps/place/"]', timeout=10000)
        except:
            print("Timeout")
            return []
            
        time.sleep(3)
        
        results = []
        # get all place links
        elements = page.query_selector_all('a[href*="/maps/place/"]')
        print(f"Found {len(elements)} items")
        
        for i in range(min(5, len(elements))):
            el = elements[i]
            label = el.get_attribute("aria-label")
            href = el.get_attribute("href")
            
            # extract basic from the list item?
            # actually we can click the element to view details
            try:
                el.click()
                time.sleep(2)
                # wait for detail pane
                page.wait_for_selector('h1', timeout=5000)
                
                # scrape info from detail pane
                # Phone: button[data-item-id^="phone:tel:"]
                phone_el = page.query_selector('button[data-item-id^="phone:tel:"]')
                phone = phone_el.get_attribute("aria-label") if phone_el else ""
                if phone and "Phone number: " in phone:
                    phone = phone.replace("Phone number: ", "")
                    
                # Website: a[data-item-id="authority"]
                web_el = page.query_selector('a[data-item-id="authority"]')
                website = web_el.get_attribute("href") if web_el else ""
                
                # Address: button[data-item-id="address"]
                add_el = page.query_selector('button[data-item-id="address"]')
                address = add_el.get_attribute("aria-label") if add_el else ""
                if address and "Address: " in address:
                    address = address.replace("Address: ", "")
                
                # Rating / Reviews
                # This can be extracted from aria-labels of stars or review text
                rating = 0
                reviews = 0
                
                results.append({
                    "name": label,
                    "website": website,
                    "phone": phone,
                    "address": address
                })
            except Exception as e:
                print(f"Error on {label}: {e}")
                
        browser.close()
        return results

if __name__ == "__main__":
    print(json.dumps(scrape_gmaps_full("plumbers in Huntsville Alabama"), indent=2))

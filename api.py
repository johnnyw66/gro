import re
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

try:
    import secret
except Exception as e:
    print(f"Trouble: {e}")
# --- CONFIGURATION ---

def generate_years(start_year, end_year):
    """Generates a list of years from start to end (inclusive) as strings."""
    return [str(year) for year in range(start_year, end_year + 1)]

def parse_rows_from_html(html_text, search_year, event_type):
    results = []
    soup = BeautifulSoup(html_text, "html.parser")
    # --- STEP 1: Find the CORRECT Table ---
    # We look for the LAST table that specifically contains the header text "Name:" 
    # to avoid the 5 ghost rows at the top of the page.
    target_table = None
    all_tables = soup.find_all("table")
    
    for table in all_tables:
        if "Name:" in table.get_text():
            target_table = table
            
    if not target_table:
        return results
        
    rows = target_table.find_all("tr")
    
    # --- STEP 2: Parse only the data rows ---
    for i in range(len(rows)):
        # Still using the Radio Button as the "Record Start" anchor
        radio = rows[i].find("input", {"type": "radio"})
        
        if radio:
            cols = rows[i].find_all("td")
            
            # Extract data from columns
            raw_name = cols[1].get_text(separator=" ", strip=True) if len(cols) > 1 else ""
            name = re.sub(r'\s+', ' ', raw_name).strip()
            
            # Skip the label row "Name:" if it happens to have a radio button
            if not name or name.lower() == "name:":
                continue

            raw_age = cols[2].get_text(strip=True) if len(cols) > 2 else "0"
            # Extract only digits (handles "72" or "Age: 72")
            age_match = re.search(r'(\d+)', raw_age)
            age = age_match.group(1) if age_match else "0"
            
            # The next row is our Reference
            if i + 1 < len(rows):
                ref_row = rows[i + 1]
                ref_text = ref_row.get_text(separator=" ", strip=True)
                ref_text = re.sub(r'\s+', ' ', ref_text).strip()
            else:
                ref_text = "No Ref"
            
            event_label = "AgeAtDeath" if 'Death' in event_type  else "Mother"
            event_value = age if 'Death' in event_type else cols[2].get_text(strip=True)

            results.append({
                "SearchYear": search_year,
                "Name": name,
                 event_label: event_value, 
                "GRO_Ref": ref_text.replace("Order this entry as a:", "").strip()
            })
                
    return results


def run_production_search(surname, forename, gender, target_years, event_type = 'Death'):
    final_output = []
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        try:
            # --- STEP 1: AUTHENTICATION ---
            print("--- Step 1: Logging In ---")
            page.goto("https://www.gro.gov.uk/gro/content/certificates/login.asp")
            page.fill('#username', secret.USERNAME)
            page.fill('#password', secret.PASSWORD)
            page.click('input[name="Submit"]')
            page.wait_for_selector('text=Logout', timeout=15000)
            print("Session active.")

            # --- STEP 2: YEAR VALIDATION ---
            page.goto("https://www.gro.gov.uk/gro/content/certificates/indexes_search.asp")
            page.check(f'input#EW_{event_type}')
            page.wait_for_timeout(1000) # Buffer for JS dropdown population

            available_years = page.eval_on_selector_all(
                "select#Year option", 
                "options => options.map(opt => opt.value)"
            )
            valid_years = [y for y in target_years if str(y) in available_years and str(y).strip() != ""]
            print(f"Validated {len(valid_years)} years for search.\n")

            # --- STEP 3: SEARCH & PAGINATION LOOP ---
            for year in valid_years:
                print(f"Processing {year}...", end=" ", flush=True)
                
                # Ensure fresh form state
                page.goto("https://www.gro.gov.uk/gro/content/certificates/indexes_search.asp")
                page.check(f'input#EW_{event_type}')
                page.select_option('select#Year', year)
                page.wait_for_timeout(1500) # Wait for page re-indexing
                
                page.fill('input#Surname', surname)
                page.fill('input#Forename1', forename)

                page.select_option('select#Gender', gender)

                if ('Birth' in event_type):
                    page.fill('input#MothersSurname','')

                # TRIGGER FIRST SEARCH
                with page.expect_response(lambda r: "indexes_search.asp" in r.url and r.status == 200) as resp:
                    page.click('input#Submit')
                
                # Parse Page 1
                page_1_html = resp.value.text()
                year_results = parse_rows_from_html(page_1_html, year, event_type)
                
                # Check for Numbered Pagination Buttons (e.g., 1, 2, 3...)
                # We target inputs/links that are purely numeric
                pagination_locators = page.locator("input[type='submit'], a").filter(has_text=re.compile(r"^\d+$"))
                total_pages = pagination_locators.count()

                if total_pages > 1:
                    print(f"(Found {total_pages} pages)", end=" ", flush=True)
                    # Loop starts from index 1 (Page 2) because we already have Page 1
                    for i in range(1, total_pages):
                        print("CLICK ON PAGE", i)
                        with page.expect_response(lambda r: "indexes_search.asp" in r.url and r.status == 200) as p_resp:
                            pagination_locators.nth(i).click(force=True)
                        
                        year_results.extend(parse_rows_from_html(p_resp.value.text(), year, event_type))
                        page.wait_for_timeout(500)

                print(f"Found {len(year_results)} records.")
                final_output.extend(year_results)

            return final_output

        except Exception as e:
            print(f"\n[CRITICAL ERROR]: {e}")
            page.screenshot(path="debug_error.png")
            return final_output
        finally:
            browser.close()



    
def filter_by_birth_year(results, target_birth, window=2):
    """Filters results to target_birth +/- window. Handles full DOB strings."""
    filtered = []
    for r in results:
        # Regex finds 4 digits (the year) in strings like '1950' or '12 May 1950'
        match = re.search(r'(\d{4})', r['SearchYear'])
        if match:
            extracted_year = int(match.group(1))
            if abs(extracted_year - target_birth) <= window:
                filtered.append(r)
    return filtered

# --- EXECUTION ---
if __name__ == "__main__":
    SURNAME_TO_FIND = "Salt"
    FORENAME_TO_FIND = ""  # Blank for wide search
    GENDER_TO_FIND = "M"
    TARGET_BIRTH_YEAR = 1938
    event = "Death"

    # 1. Define Year Range
    years_to_search = generate_years(1874, 1874)

    # 2. Run the Main Search Engine
    raw_results = run_production_search(SURNAME_TO_FIND, FORENAME_TO_FIND, GENDER_TO_FIND, years_to_search, event_type=event)
    #print(raw_results)
    for r in raw_results:
        print(r)



    # 3. Apply the Mathematical Filter
    filtered_results = filter_by_birth_year(raw_results, TARGET_BIRTH_YEAR, window=2)

    # 4. Display Final Array
    print("\n" + "="*80)
    print(f"FINAL REPORT: {len(filtered_results)} RECORDS MATCHING BIRTH ~{TARGET_BIRTH_YEAR}")
    print("="*80)

    for item in filtered_results:
        yearBorn = int(item['AgeAtDeath']) 
        if (yearBorn < 1000):
            yearBorn = int(item['SearchYear']) - yearBorn
        age = int(item['SearchYear']) - yearBorn

        print(f"Event {event} Year {item['SearchYear']} {item['Name']} (Born: {yearBorn}) Age {age}")
        print(f"   {item['GRO_Ref']}\n")


## Requirement
- Build Python application that navigates the browser using the Vision LLM to navigate



## Code concept not final
- Here is a production-ready blueprint using Python and Playwright (Async) to implement the visual navigation loop with the gemma-4-31b-it vision model.

System Architecture
The script operates entirely on pixel coordinates. Playwright opens a headless instance of Chromium, captures a screenshot, feeds it to the Gemma model, and translates the model's structural JSON response back into mouse actions.

---
import asyncio
import json
from playwright.async_api import async_playwright
from google import genai
from google.genai import types

# Initialize the Gemini/Gemma client
# (Ensure your GEMINI_API_KEY environment variable is configured)
client = genai.Client()

SYSTEM_INSTRUCTION = """
You are an autonomous web-navigation agent. Your task is to analyze the provided screenshot of an e-commerce website and decide the single next visual action required to achieve the user's objective.

You must reply strictly with a valid JSON object matching this schema:
{
  "action": "click" | "type" | "scroll" | "done" | "fail",
  "target_x": int,
  "target_y": int,
  "text_to_type": "string or empty",
  "reasoning": "Brief explanation of your chosen action"
}

Rules:
1. Coordinates (target_x, target_y) must be the exact center of the element relative to the 1920x1080 screenshot viewport.
2. If an intrusive modal, newsletter subscription pop-up, or cookie consent banner is blocking the content, your immediate priority action is to click its close button.
3. Use 'scroll' if the product or element is not visible on the current screen viewport.
"""

async def navigate_ecommerce(objective: str, start_url: str):
    async with async_playwright() as p:
        # 1. Launch a clean browser matching standard desktop dimensions
        browser = await p.chromium.launch(headless=False) # Set to True in production
        context = await browser.new_context(viewport={"width": 1920, "height": 1080})
        page = await context.new_page()
        
        print(f"[*] Navigating to: {start_url}")
        await page.goto(start_url)
        # Give the core elements time to load cleanly
        await page.wait_for_load_state("networkidle")
        
        max_steps = 15
        for step in range(max_steps):
            print(f"\n--- Step {step + 1} ---")
            
            # 2. Capture the exact visual viewport state
            screenshot_bytes = await page.screenshot(full_page=False)
            
            prompt = f"User Objective: {objective}\nAnalyze the current screen and output the next logical step."
            
            # 3. Query Gemma-4-31B-IT with Structured Outputs
            try:
                response = client.models.generate_content(
                    model='gemma-4-31b-it',
                    contents=[
                        types.Part.from_bytes(data=screenshot_bytes, mime_type='image/png'),
                        prompt
                    ],
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_INSTRUCTION,
                        response_mime_type="application/json",
                        temperature=0.1 # Keep determinism high
                    )
                )
                
                # Parse structured decision
                decision = json.loads(response.text)
                print(f"[Reasoning]: {decision.get('reasoning')}")
                
                action_type = decision.get("action")
                x = decision.get("target_x")
                y = decision.get("target_y")
                text = decision.get("text_to_type", "")
                
                # 4. Action Execution Layer
                if action_type == "done":
                    print("[+] Objective successfully achieved!")
                    break
                elif action_type == "fail":
                    print("[-] Agent declared mission failure.")
                    break
                    
                elif action_type == "click":
                    print(f"[*] Clicking coordinates: ({x}, {y})")
                    await page.mouse.click(x, y)
                    
                elif action_type == "type":
                    print(f"[*] Typing into input at ({x}, {y}): '{text}'")
                    await page.mouse.click(x, y)
                    await page.keyboard.type(text)
                    await page.keyboard.press("Enter")
                    
                elif action_type == "scroll":
                    print("[*] Target not found in viewport. Scrolling down...")
                    # Scroll down by half a viewport screen height
                    await page.evaluate("window.scrollBy(0, 540)")
                
                # Allow network layer and client-side scripts to settle
                await page.wait_for_timeout(2500)
                
            except Exception as e:
                print(f"[!] Error handling step: {e}")
                break
                
        await browser.close()

# Run the execution loop
if __name__ == "__main__":
    TARGET_OBJECTIVE = "Find a mechanical keyboard under $100 and click it to view details."
    START_SITE = "https://www.amazon.com" # Swap with your target e-commerce site
    
    asyncio.run(navigate_ecommerce(TARGET_OBJECTIVE, START_SITE))
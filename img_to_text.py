import requests

API_KEY = "K82952903188957"

def extract_text_from_image(image_path):
    url = "https://api.ocr.space/parse/image"

    with open(image_path, 'rb') as f:
        response = requests.post(
            url,
            files={"file": f},
            data={
                "apikey": API_KEY,
                "language": "eng",
                "isOverlayRequired": False
            }
        )

    result = response.json()

    # Debug (optional)
    # print(result)

    if result.get("IsErroredOnProcessing"):
        return "Error: " + str(result.get("ErrorMessage"))

    parsed_results = result.get("ParsedResults")

    if parsed_results:
        return parsed_results[0].get("ParsedText", "").strip()

    return "No text found"
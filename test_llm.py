import os
import json
from google import genai
from pydantic import BaseModel

# Importujemy nasz schemat z głównego pliku
from scraper import KierunekStudiow

def main():
    # 1. Wczytujemy zescrapowany tekst z dysku
    with open("scraped_pja.md", "r", encoding="utf-8") as f:
        text = f.read()

    print("🔄 Inicjalizacja nowego klienta Google GenAI...")
    # Nowy klient automatycznie zaciąga zmienną GEMINI_API_KEY z systemu
    client = genai.Client()

    prompt = f"""Jesteś ekspertem ds. rekrutacji. Przeanalizuj tekst ze strony uczelni 
    i wyciągnij informacje o kierunku studiów, ignorując nawigację i stopki. 
    Zwróć dane precyzyjnie dopasowane do schematu.
    
    Oto tekst do analizy:
    {text}
    """

    print("🧠 Wysyłam dane do modelu gemini-2.5-flash...")
    
    # 2. Wysyłamy zapytanie wymuszając nasz schemat Pydantic
    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=prompt,
        config={
            'response_mime_type': 'application/json',
            'response_schema': KierunekStudiow,
            'temperature': 0.1 
        },
    )

    # 3. Wyświetlamy piękny JSON
    print("\n✅ Gotowe! Oto wynik działania AI:\n")
    parsed_json = json.loads(response.text)
    print(json.dumps(parsed_json, indent=4, ensure_ascii=False))

if __name__ == "__main__":
    main()
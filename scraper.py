import os
import json
import asyncio
from typing import List, Optional, Literal
from pydantic import BaseModel, Field
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
from crawl4ai.extraction_strategy import LLMExtractionStrategy

# 1. Definicja Schematu Danych (Pydantic)
class KierunekStudiow(BaseModel):
    kierunek: str = Field(description="Nazwa kierunku studiów")
    stopien: Literal["1_stopnia", "2_stopnia", "jednolite_magisterskie"] = Field(description="Stopień studiów")
    tytul: Literal["licencjat", "inżynier", "magister"] = Field(description="Uzyskiwany tytuł zawodowy")
    tryb: List[Literal["stacjonarne", "niestacjonarne"]] = Field(description="Dostępne tryby studiów")
    semestry: Optional[int] = Field(None, description="Liczba semestrów w formie liczby całkowitej")
    wydzial: Optional[str] = Field(None, description="Wydział prowadzący kierunek")
    jezyk: str = Field(default="polski", description="Język wykładowy")
    specjalizacje: List[str] = Field(default_factory=list, description="Lista dostępnych specjalizacji")
    rekrutacja: List[str] = Field(default_factory=list, description="Wymagane przedmioty maturalne lub zasady rekrutacji")
    czesne: Optional[str] = Field(None, description="Koszty studiów. Jeśli brak informacji lub studia są darmowe, zwróć null")
    opis: str = Field(description="Krótkie, 2-3 zdaniowe podsumowanie profilu kierunku na podstawie tekstu")

async def extract_course_info(url: str):
    print(f"🕵️ Rozpoczynam skanowanie: {url}")
    
    # WYŁĄCZAMY LLM NA CZAS TESTÓW BEZ KLUCZA
    # llm_strategy = LLMExtractionStrategy(...)

    browser_config = BrowserConfig(
        headless=True,
        java_script_enabled=True, 
    )
    
    # Usuwamy extraction_strategy z konfiguracji
    run_config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS, 
        word_count_threshold=10, 
    )

    async with AsyncWebCrawler(config=browser_config) as crawler:
        result = await crawler.arun(url=url, config=run_config)
        
        if result.success:
            print("\n✅ Pobieranie zakończone sukcesem! Oto wyczyszczony tekst (Markdown):\n")
            # Wyświetlamy pierwsze 1000 znaków, żeby nie zalać konsoli całym tekstem ze strony
            print(result.markdown[:1000])
            print("\n... [reszta tekstu ucięta dla czytelności] ...")
        else:
            print(f"\n❌ Błąd podczas skanowania: {result.error_message}")

if __name__ == "__main__":
    # Testowy link (możesz podmienić na dowolny link do konkretnego kierunku z dowolnej polskiej uczelni)
    TEST_URL = "https://www.pja.edu.pl/informatyka/licencjackie/" 
    
    asyncio.run(extract_course_info(TEST_URL))
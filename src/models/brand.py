from pydantic import BaseModel


class ColorPalette(BaseModel):
    primary: str | None = None          # hex code, e.g. "#1A2B3C"
    secondary: str | None = None
    accent: str | None = None
    background: str | None = None
    text: str | None = None
    raw_values: list[str] = []          # Any additional colors mentioned by client


class ToneOfVoice(BaseModel):
    descriptors: list[str] = []         # e.g. ["professional", "warm", "approachable"]
    dos: list[str] = []                 # Writing guidelines — what to do
    donts: list[str] = []               # Writing guidelines — what to avoid
    example_copy: list[str] = []        # Sample sentences that capture the brand voice


class BrandGuidelines(BaseModel):
    client_id: str
    notion_page_id: str | None = None   # ID of the Brand Guidelines page in Notion

    colors: ColorPalette | None = None
    tone_of_voice: ToneOfVoice | None = None

    primary_font: str | None = None
    secondary_font: str | None = None

    logo_assets: list[str] = []         # Notion file URLs or Google Drive links
    inspiration_urls: list[str] = []    # Website URLs the client likes
    competitor_urls: list[str] = []     # Competitor sites for reference

    raw_guidelines_text: str | None = None  # Fallback for unstructured brand notes

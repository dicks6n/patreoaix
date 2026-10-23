import feedparser
import requests
from bs4 import BeautifulSoup
from urllib.parse import quote
from transformers import AutoTokenizer, AutoModelForSequenceClassification
import torch
import torch.nn.functional as F

# Initialize FinBERT Model & Tokenizer globally (lazy loaded)
_FINBERT_TOKENIZER = None
_FINBERT_MODEL = None

def get_finbert():
    global _FINBERT_TOKENIZER, _FINBERT_MODEL
    if _FINBERT_TOKENIZER is None or _FINBERT_MODEL is None:
        _FINBERT_TOKENIZER = AutoTokenizer.from_pretrained("yiyanghkust/finbert-tone")
        _FINBERT_MODEL = AutoModelForSequenceClassification.from_pretrained("yiyanghkust/finbert-tone")
    return _FINBERT_TOKENIZER, _FINBERT_MODEL

LABELS = ['Positive', 'Negative', 'Neutral']


def fetch_news(query, num_articles=10):
    """Fetches news via Google RSS feed."""
    rss_url = f"https://news.google.com/rss/search?q={quote(query)}"
    feed = feedparser.parse(rss_url)
    news_items = feed.entries[:num_articles]

    articles = []
    for item in news_items:
        articles.append({
            "title": item.title,
            "link": item.link,
            "published": getattr(item, 'published', '')
        })
    return articles


def analyze_text_finbert(text):
    """Analyzes financial headline text using FinBERT."""
    if not text.strip():
        return 0.0, 'Neutral'

    tokenizer, model = get_finbert()
    inputs = tokenizer(text, return_tensors="pt", truncation=True, max_length=512)
    
    with torch.no_grad():
        outputs = model(**inputs)

    probs = F.softmax(outputs.logits, dim=1).numpy()[0]
    pos_score, neg_score, neu_score = probs[0], probs[1], probs[2]

    # Polarity Score (-1.0 to +1.0)
    polarity = float(pos_score - neg_score)
    
    if polarity > 0.15:
        sentiment = 'Positive'
    elif polarity < -0.15:
        sentiment = 'Negative'
    else:
        sentiment = 'Neutral'

    return polarity, sentiment


def generate_trade_signal(asset_symbol="XAU/USD", search_queries=None, articles_per_query=5):
    """
    Main pipeline function: fetches articles, calculates sentiment, 
    and outputs BUY/SELL trade signals.
    """
    if search_queries is None:
        search_queries = ["gold price market forecast", "gold investment news"]

    all_articles = []
    for query in search_queries:
        all_articles.extend(fetch_news(query, num_articles=articles_per_query))

    if not all_articles:
        return None

    total_polarity = 0.0
    positive_count = 0
    negative_count = 0
    neutral_count = 0

    analyzed_articles = []

    for item in all_articles:
        polarity, sentiment = analyze_text_finbert(item['title'])
        total_polarity += polarity

        if sentiment == 'Positive':
            positive_count += 1
        elif sentiment == 'Negative':
            negative_count += 1
        else:
            neutral_count += 1

        analyzed_articles.append({
            "title": item['title'],
            "link": item['link'],
            "published": item['published'],
            "sentiment": sentiment,
            "polarity": polarity
        })

    total = len(analyzed_articles)
    composite_score = total_polarity / total if total > 0 else 0.0

    # Determine Trade Signal Thresholds
    # Composite score ranges from -1.0 to +1.0
    if composite_score >= 0.20:
        signal = 'BUY'
    elif composite_score <= -0.20:
        signal = 'SELL'
    else:
        signal = 'NEUTRAL'

    # Calculate Signal Confidence %
    max_agreement = max(positive_count, negative_count, neutral_count)
    confidence = round((max_agreement / total) * 100, 1)

    return {
        "asset_symbol": asset_symbol,
        "composite_score": round(composite_score, 3),
        "signal": signal,
        "confidence": confidence,
        "total_analyzed": total,
        "positive_count": positive_count,
        "negative_count": negative_count,
        "neutral_count": neutral_count,
        "articles": analyzed_articles
    }

from .models import NewsArticle
# Import your specific sentiment service
from store.services.sentiment_service import analyze_news_sentiment 

def fetch_financial_news():
    """
    Fetches financial news via API and processes sentiment 
    using the local sentiment_service.
    """
    api_key = "YOUR_API_KEY"
    url = f"https://finnhub.io/api/v1/news?category=general&token={api_key}"
    
    response = requests.get(url)
    
    if response.status_code == 200:
        articles = response.json()
        
        for art in articles[:10]: # Process the latest 10 articles
            
            # 1. Use your existing sentiment service to get the sentiment
            # This calls the file located at /Users/mac/Desktop/trading/store/services/sentiment_service.py
            sentiment_data = analyze_news_sentiment(art['summary'])
            
            # 2. Update or create the database record
            NewsArticle.objects.update_or_create(
                title=art['headline'],
                defaults={
                    'summary': art['summary'],
                    'source': art['source'],
                    'category': 'Forex', 
                    # Use the result from your custom service
                    'sentiment': sentiment_data.get('label', 'Neutral'), 
                    'timestamp': art['datetime']
                }
            )
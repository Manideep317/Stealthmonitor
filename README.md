https://github.com/user-attachments/assets/8cba337f-22b9-4211-b8e0-ac951b4f4be9

Stealth Monitor
An autonomous, intelligent competitor monitoring and price-tracking system. Stealth Monitor combines AI-powered web discovery, a self-healing multi-tier scraping engine, an agentic onboarding workflow with Human-In-The-Loop (HITL) intelligence, and multi-channel conversational alerts across Telegram and Email.

How It Works
Discovery & Onboarding — Provide a niche description (e.g., "premium dog toys DTC brands") or a competitor name. Stealth Monitor finds competitors via Exa Deep Search, discovers and validates their pricing pages, and registers them for monitoring.

Autonomous Agent with HITL — An LLM agent decides when to register targets autonomously and when to ask a human for input on ambiguities such as marketplace listings, unlabelled prices, or large catalogs.

Adaptive Continuous Polling — Monitors registered targets using a self-healing 5-tier extraction cascade. Winning extraction strategies are cached for high-performance, zero-browser subsequent checks.

Natural Language Alerts — On detecting a price or availability change, an LLM synthesizes the diff into a clean 2–3 sentence notification dispatched via Email and Telegram. Users can also message the bot directly to trigger competitor research on the fly.

The 5-Tier Extraction Cascade
Tier	Strategy	Description
1	Shopify API	Checks .json endpoints for full variant data, prices, and stock status
2	JSON-LD Schema	Extracts schema.org Product metadata from <script type="application/ld+json">
3	OpenGraph & Microdata	Parses <meta property="product:price:amount"> and [itemprop="price"] tags
4	Playwright Network Sniffing	Launches headless Chromium to intercept XHR/fetch responses or parse rendered pricing tables
5	SHA-256 Content Hash	Fallback checksum for unstructured pages
Once a strategy succeeds, it is cached. If a target site changes its structure and extraction fails, the engine automatically falls back to the full cascade to discover the new working strategy.

Tech Stack
AI & LLM — Featherless AI (Kimi-K2-Instruct) via OpenAI-compatible SDK for tool-calling agent loops and alert summarization
Search & Discovery — Exa API for deep semantic discovery of niche competitors
Communications — Caspian SDK for bi-directional Telegram and Email messaging
Browser Automation — Playwright (async Chromium), BeautifulSoup4
Backend & Storage — Flask, SQLite (WAL mode)
Runtime — Python 3.13+, uv
Key Features
End-to-End Competitor Discovery — From a simple prompt to a curated list of live competitors with validated pricing pages
Autonomous & HITL Flexibility — Operates standalone or intelligently asks humans when encountering ambiguity
Conversational 2-Way Bot — Start competitor tracking directly via Telegram or Email with real-time progress updates
Cost-Optimized Scraping — Minimizes headless browser overhead by caching lightweight HTTP extraction strategies
LLM-Summarized Alerts — Human-readable notifications instead of raw JSON diffs
Setup
Prerequisites
Python 3.13+
API Keys: Exa, Featherless AI, Caspian (and optionally a Telegram bot token)
Installation
bash


# Install dependencies
uv sync
# Install Playwright browser binaries
uv run playwright install chromium
# Configure environment variables
cp .env.example .env
# Edit .env with your API keys
Usage
Onboard Targets
Via AI Agent (HITL):

bash


uv run python agent.py "premium dog toys DTC brands"
Via Deterministic CLI:

bash


uv run python selector.py
Start the Alerting Server
bash


uv run python app.py
Start Continuous Monitoring
bash


uv run python watcher.py
Interactive Messaging
Send a message to your configured Caspian Telegram bot or Email address to trigger competitor tracking directly over chat.

Project Structure


├── agent.py       # AI agent with HITL onboarding and function calling
├── alerts.py      # Unified Caspian SDK communications layer
├── app.py         # Flask webhook gateway and REST server
├── config.py      # Centralized configuration and environment settings
├── db.py          # SQLite persistence layer (targets, line items, change log)
├── discover.py    # Exa-powered competitor discovery
├── selector.py    # Deterministic CLI onboarding script
├── watcher.py     # Universal polling and change detection engine
├── tools/         # Standalone utilities (ETag checker, page operator)
└── monitor.db     # SQLite database (auto-created)
API Endpoints
Endpoint	Method	Description
/health	GET	Health check
/targets	GET	List all monitored targets
/changes	GET	View change history log
/webhook/change-detected	POST	Receive raw change diffs and trigger alerts
License
MIT

Feel free to tweak the wording, add badges, or adjust any section to your liking!



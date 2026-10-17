# MASTER ARCHITECTURAL BLUEPRINT: QUANTITATIVE MULTI-MODEL RACING SYSTEM
# ==============================================================================
# VERSION: 2.5 (Production Specification)
# SYSTEM FOCUS: Anti-Favourite Value Identification & Smart-Money Price Velocity
# OVERSEEING CONTEXTS: Fully Indexed Across Project Workspace Memories
# ==============================================================================

[SECTION 1: DISCIPLINED MULTI-MODEL TEAM ROLES]

The system explicitly rejects monolithic AI prediction logic. It operates as an 
unbiased, decentralised processing loop where each entity is mathematically bound:

1. THE SYSTEMS INTEGRATION & LIVE DATA HARVESTER (Engine Core Interface)
   - Navigates the live global racing feeds to extract active upcoming fields.
   - Strips away layout formatting errors to present pristine data strings.
   - Enforces process continuity, maintains folder integrity, and blocks context drift.

2. LOCAL MATHEMATICAL CORE (Python Runtime Terminal Engine)
   - Serves as the immutable quantitative gatekeeper.
   - Parses incoming single-line horizontal text strings using regular expressions.
   - Runs Shin's De-Vig Optimization Algorithm to eliminate Favourite-Longshot Bias, 
     revealing the unvarnished mathematical truth of the fair market percentage.
   - Tracks real-time price velocity anomalies (Steamers vs. Drifters).
   - Defeats survivorship bias by logging EVERY single flagged overlay into a 
     permanent CSV performance file, irrespective of whether the horse wins or loses.

3. QUANT SOFTWARE ARCHITECT (Claude Project Workspace Model)
   - Owns code safety, file paths, and dependency execution (main.py, scraper.py).
   - Rejects visual "cheerleading" features in favour of code-level data integrity.
   - Programs upstream input guards to catch hallucinated prices before they run.

4. LIVE RISK-ADJUSTMENT & AUDIT GATE (Microsoft Copilot UI Browser Bridge)
   - Operates exclusively as a Live-Web Risk Manager sitting sitting on top of Python.
   - Employs live Bing engines to cross-reference our local overround tables with 
     paddock metadata: late-breaking scratchings, jockey changes, and track updates.
   - Filters the card spread down into clean, execution-ready betting tiers.

# ==============================================================================
# [SECTION 2: MASTER PROGRAMMATIC FILE SPECIFICATIONS]
# ==============================================================================

--- SCRIPT MODULE 1: scraper.py (Shin's De-Vig Optimizer & Data Parse Layer) ---
- Core Duty: Strip bookmaker padding disproportionately off longshot/mid-priced columns.
- Critical Code Functions Embedded:
  * parse_card_text(): Strips whitespace and isolates horse entries matching regex blocks.
  * de_vig_shin(): Solves for the transaction parameter 'z' non-linearly to destroy bias.
  * Input Guards: Instantly throws terminal error blocks if an ingested price implies a 
    fictional >95% probability or if a full-field overround falls below a broken 99%.

--- SCRIPT MODULE 2: main.py (The Orchestrator, Non-Survivorship CSV Logger) ---
- Core Duty: Manage local data collection loops and auto-compile clipboard prompts.
- Critical Code Functions Embedded:
  * calculate_composite_confidence(): Formulates 1-10 rankings based on mathematical edges.
  * log_all_selections_to_csv(): Registers every flagged selection as 'PENDING'.
  * Auto-Clipboard: Bundles the real math findings into a clear text prompt using 'pyperclip'.

--- SCRIPT MODULE 3: analyse.py (Standalone Forensic Portfolio Auditor) ---
- Core Duty: Track long-term Closing Line Value (CLV), strike rate, and net ROI %.
- Execution Mandate: Provides objective data forensic audits once 100+ settled bets register.

# ==============================================================================
# [SECTION 3: STEP-BY-STEP TERMINAL MANAGEMENT OPERATIONAL LAYOUT]
# ==============================================================================

Whenever launching a completely fresh chat workspace, follow this exact sequence 
to fire up the local terminal core:

STEP 1: ACTIVATE ISOLATED DEVELOPMENT SPACE
Open Windows PowerShell and paste these three commands sequentially:
  cd $HOME\Documents\HorseRacingEngine
  Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process
  .\venv\Scripts\Activate.ps1

* Verification Check: The prompt line MUST prefix with the brackets tag string '(venv)'.

STEP 2: RUN THE QUANT ENGINE ORCHESTRATOR
  python main.py
* Note: When asked for an Anthropic API Key, simply press [Enter] to skip and run for free.

STEP 3: DEFEAT CONSOLE TEXT HANGS WITH THE EOF INTERRUPT
Pasting large single-line strings can sometimes stall PowerShell buffers. Force execution:
  1. Paste the horizontal data row from your Harvester.
  2. Type the word ALL_DONE on a fresh line and press [Enter].
  3. Press [Ctrl + Z] on your keyboard (the text string ^Z will appear on screen).
  4. Press [Enter] one last time to force-execute the math tables immediately.

STEP 4: HAND OFF THE BALANCED PAYLOAD TO THE COPILOT AUDIT GATE
  1. Open a browser tab to ://microsoft.com.
  2. Press [Ctrl + V] to paste the automatically generated clipboard prompt block.
  3. Let Copilot review late scratches, confirm the active steam, and print the bets list.

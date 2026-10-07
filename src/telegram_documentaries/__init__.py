"""The Telegram Documentaries.

Phase 1 delivers the gateway only: configuration, logging, a typed inbound
contract and a `/start` handler. Modules are imported lazily by their callers;
nothing is re-exported here, so importing the package stays free of side
effects and of the Telegram or Gemini stack.
"""

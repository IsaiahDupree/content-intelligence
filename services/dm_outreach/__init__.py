"""
DM Outreach Package
Systematic prospect discovery and trust-building outreach.
"""

from .dm_list_manager import (
    DMListManager,
    Prospect,
    DMListEntry,
    Offer,
    ProspectStatus,
    OutreachPhase,
    DiscoverySource,
    get_dm_list_manager
)

from .outreach_sequencer import (
    OutreachSequencer,
    MessageSuggestion,
    get_outreach_sequencer,
    TEMPLATES
)

from .prospect_finder import (
    ProspectFinder,
    get_prospect_finder,
    ACCOUNT_OFFERS
)

from .dm_sender import (
    DMSender,
    get_dm_sender
)

__all__ = [
    # Manager
    "DMListManager",
    "Prospect",
    "DMListEntry",
    "Offer",
    "ProspectStatus",
    "OutreachPhase",
    "DiscoverySource",
    "get_dm_list_manager",
    
    # Sequencer
    "OutreachSequencer",
    "MessageSuggestion",
    "get_outreach_sequencer",
    "TEMPLATES",
    
    # Finder
    "ProspectFinder",
    "get_prospect_finder",
    "ACCOUNT_OFFERS",
    
    # Sender
    "DMSender",
    "get_dm_sender"
]

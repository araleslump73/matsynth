"""Validatore e migrazione di riferimento per docs/contracts.md (sez. 3-6).

Nella fase `ui-persistence` la specifica eseguibile e' stata portata nel
modulo reale `home/matteo/matsynth_web/project_store.py` (stessi codici e
percorsi d'errore, piu' le regole D3 sugli ID clip per evento).
tests/test_contract_examples.py importa ora il modulo reale; questo file
resta solo come alias per eventuali script che lo importavano.
"""

import os
import sys

_APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'home', 'matteo', 'matsynth_web')
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

from project_store import *          # noqa: E402,F401,F403
from project_store import (          # noqa: E402,F401  (nomi con underscore usati dai vecchi script)
    ContractError, _pair_fifo_unpaired, owned_indices, migrate_legacy_preset,
    validate_part, validate_scene, validate_project, default_part)

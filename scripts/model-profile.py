"""Emit one verified local profile for the PowerShell launcher's arguments."""
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from research_crawler.core.model_profiles import installed_profile
print(json.dumps(installed_profile(ROOT,sys.argv[1])))

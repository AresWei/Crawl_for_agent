"""Source-derived platform modules using one application runtime."""
from importlib import import_module

UPSTREAM_COMMIT = "54a3d92412f4b20c06b45f1d542b78dd92049d6d"
PLATFORMS = {"xhs": ("小红书", "xiaohongshu"), "dy": ("抖音", "douyin"),
             "ks": ("快手", "kuaishou"), "bili": ("哔哩哔哩", "bilibili"),
             "wb": ("微博", "weibo"), "tieba": ("百度贴吧", "tieba"), "zhihu": ("知乎", "zhihu")}


def module_for(platform):
    if platform not in PLATFORMS:
        raise ValueError("Unknown platform")
    return import_module("." + PLATFORMS[platform][1], __name__)


def capability_registry():
    result = {}
    for code, (name, _) in PLATFORMS.items():
        entry = {"name": name, "upstream_commit": UPSTREAM_COMMIT, "live_validation": "not_validated"}
        try:
            module = module_for(code)
            entry.update(module.CAPABILITIES)
            entry["implemented"] = callable(getattr(module, "collect", None))
            entry["status"] = "source_migrated_unverified" if entry["implemented"] else "pending_migration"
        except ImportError:
            entry.update(status="dependency_missing", implemented=False)
        if code == "bili":
            entry["live_validation"] = "anonymous_detail_and_10_root_comments_2026-11-06"
            entry["validation_limits"] = ["single_public_video", "authenticated_account_untested", "other_modes_live_untested"]
        result[code] = entry
    return result

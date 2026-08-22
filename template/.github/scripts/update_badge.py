#!/usr/bin/env python3
import json
import os
import sys
import urllib.request


def update_badge(gist_id, token, filename, label, message, color):
    badge = {
        "schemaVersion": 1,
        "label": label,
        "message": message,
        "color": color,
    }
    payload = {
        "files": {
            filename: {
                "content": json.dumps(badge, indent=2),
            }
        }
    }
    req = urllib.request.Request(
        f"https://api.github.com/gists/{gist_id}",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        method="PATCH",
    )
    with urllib.request.urlopen(req):
        pass


def get_default_badge(label, message, color):
    return {
        "schemaVersion": 1,
        "label": label,
        "message": message,
        "color": color,
    }


def reset_all_badges(gist_id, token):
    files = {
        "setup-status.json": {
            "content": json.dumps(
                get_default_badge("Setup", "required", "yellow"), indent=2
            )
        },
        "format-status.json": {
            "content": json.dumps(
                get_default_badge("Resume Format", "pending", "inactive"),
                indent=2,
            )
        },
        "scrape-status.json": {
            "content": json.dumps(
                get_default_badge("Daily Job Scraper", "pending", "inactive"),
                indent=2,
            )
        },
        "triage-status.json": {
            "content": json.dumps(
                get_default_badge("Triage & Tailor", "pending", "inactive"),
                indent=2,
            )
        },
        "respond-status.json": {
            "content": json.dumps(
                get_default_badge("Issue Assistant", "pending", "inactive"),
                indent=2,
            )
        },
    }
    req = urllib.request.Request(
        f"https://api.github.com/gists/{gist_id}",
        data=json.dumps({"files": files}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        method="PATCH",
    )
    with urllib.request.urlopen(req):
        pass


def main():
    gist_id = os.environ.get("GIST_ID")
    token = os.environ.get("GH_TOKEN")
    if not gist_id or not token:
        print("GIST_ID or GH_TOKEN is not set. Skipping badge update.")
        return

    try:
        if len(sys.argv) == 2 and sys.argv[1] == "--reset":
            reset_all_badges(gist_id, token)
            print("Successfully reset all badges to required/pending.")
        elif len(sys.argv) == 5:
            label = sys.argv[1]
            message = sys.argv[2]
            color = sys.argv[3]
            filename = sys.argv[4]
            update_badge(gist_id, token, filename, label, message, color)
            print(f"Successfully updated badge {filename} to {message} ({color}).")
        else:
            print("Usage:")
            print("  update_badge.py --reset")
            print("  update_badge.py <label> <message> <color> <filename>")
            sys.exit(1)
    except Exception as e:
        print(f"::warning::Failed to update badge: {e}")


if __name__ == "__main__":
    main()

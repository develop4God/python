import requests
import json
from datetime import datetime
import os
import base64
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# CONFIGURATION
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
REPO_OWNER = os.getenv("REPO_OWNER", "develop4God")  # ✅ Default to develop4God

# Validate required environment variables
if not GITHUB_TOKEN:
    print("❌ ERROR: Missing required environment variables!")
    print("📝 Please create a .env file with:")
    print("   GITHUB_TOKEN=your_token_here")
    print("   REPO_OWNER=develop4God  (optional, defaults to develop4God)")
    exit(1)

HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json"
}

# Important file extensions for Flutter/Dart
IMPORTANT_EXTENSIONS = {
    '.dart', '.json', '.yaml', '.yml'
}

# Important folders - ONLY specified ones
IMPORTANT_FOLDERS = {
    'lib', 'i18n', 'test'
}

# Specific root files to include
ROOT_FILES_TO_INCLUDE = {
    'pubspec.yml', 'pubspec.yaml'
}


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def make_request(url, params=None):
    """Helper to make GET requests with error handling"""
    try:
        response = requests.get(url, headers=HEADERS, params=params)
        if response.status_code == 200:
            return response.json()
        elif response.status_code == 404:
            print(f"⚠️  Not found (404): {url}")
            return None
        elif response.status_code == 403:
            print(f"🔒 Access denied (403) — check token permissions for: {url}")
            return None
        else:
            msg = response.json().get('message', 'Unknown error')
            print(f"❌ Error {response.status_code}: {msg}")
            return None
    except Exception as e:
        print(f"❌ Request failed: {e}")
        return None


# ─────────────────────────────────────────────
# ✅ NEW: REPO SELECTION
# ─────────────────────────────────────────────

def get_user_repos(owner, per_page=50):
    """Fetch all repos for a GitHub user/org"""
    url = f"https://api.github.com/users/{owner}/repos"
    params = {"per_page": per_page, "sort": "updated", "direction": "desc", "type": "all"}
    data = make_request(url, params)
    if not data:
        # Try org endpoint as fallback
        url = f"https://api.github.com/orgs/{owner}/repos"
        data = make_request(url, params)
    return data or []


def select_repo_interactively(owner):
    """
    List repos for `owner` and let user pick one by number.
    Returns the selected repo name string, or None if cancelled.
    """
    print(f"\n🔍 Fetching repositories for '{owner}'...")
    repos = get_user_repos(owner)

    if not repos:
        print(f"❌ No repositories found for '{owner}'")
        print("💡 Check that REPO_OWNER is correct and your token has access.")
        return None

    print(f"\n📦 Repositories for '{owner}' ({len(repos)} found):")
    print("─" * 60)
    for i, repo in enumerate(repos, 1):
        visibility = "🔒" if repo.get("private") else "🌐"
        updated = datetime.fromisoformat(
            repo["updated_at"].replace("Z", "+00:00")
        ).strftime("%Y-%m-%d")
        stars = repo.get("stargazers_count", 0)
        lang = repo.get("language") or "—"
        print(f"{i:3d}. {visibility} {repo['name']:<35} ⭐{stars:<3} 🗣{lang:<12} 📅{updated}")

    print()
    try:
        sel = int(input("Select repo number: ").strip()) - 1
        if 0 <= sel < len(repos):
            chosen = repos[sel]["name"]
            print(f"✅ Selected: {chosen}")
            return chosen
        else:
            print("❌ Invalid selection")
            return None
    except (ValueError, KeyboardInterrupt):
        print("❌ Cancelled")
        return None


# ─────────────────────────────────────────────
# BRANCHES
# ─────────────────────────────────────────────

def get_branches(repo_name):
    url = f"https://api.github.com/repos/{REPO_OWNER}/{repo_name}/branches"
    data = make_request(url)
    return [b["name"] for b in data] if data else []


# ─────────────────────────────────────────────
# ✅ UPDATED: OPEN PRs ONLY
# ─────────────────────────────────────────────

def get_open_pull_requests(repo_name, per_page=50):
    """
    Get ONLY open (not merged, not closed) PRs.
    Previously mixed open+closed — now strictly open only.
    """
    url = f"https://api.github.com/repos/{REPO_OWNER}/{repo_name}/pulls"
    params = {
        "state": "open",          # ✅ open only
        "per_page": per_page,
        "sort": "updated",
        "direction": "desc"
    }
    return make_request(url, params) or []


def display_open_prs_numbered(repo_name):
    """Display open PRs with numbered selection. Returns list or None."""
    print(f"\n🔍 Fetching OPEN PRs from {REPO_OWNER}/{repo_name}...")
    open_prs = get_open_pull_requests(repo_name)

    if not open_prs:
        print(f"ℹ️  No open PRs found in '{repo_name}'")
        return None

    print(f"\n📋 Open PRs ({len(open_prs)}):")
    print("─" * 70)
    for i, pr in enumerate(open_prs, 1):
        created = datetime.fromisoformat(
            pr["created_at"].replace("Z", "+00:00")
        ).strftime("%Y-%m-%d")
        updated = datetime.fromisoformat(
            pr["updated_at"].replace("Z", "+00:00")
        ).strftime("%Y-%m-%d")

        print(f"{i:3d}. 🟢 #{pr['number']} — {pr['title'][:60]}{'...' if len(pr['title']) > 60 else ''}")
        print(f"       👤 {pr['user']['login']}  |  🌿 {pr['head']['ref']} → {pr['base']['ref']}")
        print(f"       📅 Created: {created}  |  🔄 Updated: {updated}")
        print()

    return open_prs


def display_branch_open_prs(repo_name, branch_name):
    """Show open PRs for a specific branch (no closed/merged)."""
    print(f"\n🔍 Searching OPEN PRs for branch '{branch_name}'...")
    url = f"https://api.github.com/repos/{REPO_OWNER}/{repo_name}/pulls"
    params = {
        "state": "open",
        "head": f"{REPO_OWNER}:{branch_name}",
        "per_page": 50
    }
    prs = make_request(url, params) or []

    if not prs:
        print(f"ℹ️  No open PRs found for branch '{branch_name}'")
        return []

    print(f"\n📋 Open PRs for '{branch_name}':")
    print("─" * 60)
    for i, pr in enumerate(prs, 1):
        created = datetime.fromisoformat(
            pr["created_at"].replace("Z", "+00:00")
        ).strftime("%Y-%m-%d")
        print(f"{i:2d}. 🟢 #{pr['number']} — {pr['title'][:70]}")
        print(f"     👤 {pr['user']['login']} | 📅 {created}")
        print()

    return prs


# ─────────────────────────────────────────────
# PR DETAILS
# ─────────────────────────────────────────────

def get_pr_details(repo_name, pr_number):
    """Get complete details of a specific PR including reviews and comments"""
    base = f"https://api.github.com/repos/{REPO_OWNER}/{repo_name}"
    return {
        "pr_info":        make_request(f"{base}/pulls/{pr_number}"),
        "commits":        make_request(f"{base}/pulls/{pr_number}/commits"),
        "files":          make_request(f"{base}/pulls/{pr_number}/files"),
        "reviews":        make_request(f"{base}/pulls/{pr_number}/reviews"),
        "comments":       make_request(f"{base}/pulls/{pr_number}/comments"),
        "issue_comments": make_request(f"{base}/issues/{pr_number}/comments"),
    }


def format_pr_analysis(pr_details):
    """Format PR information for analysis"""
    pr_info       = pr_details["pr_info"]
    commits       = pr_details["commits"] or []
    files         = pr_details["files"] or []
    reviews       = pr_details["reviews"] or []
    comments      = pr_details["comments"] or []
    issue_comments = pr_details["issue_comments"] or []

    analysis = f"""
🔍 PULL REQUEST ANALYSIS #{pr_info['number']}
{'='*60}

📋 GENERAL INFORMATION:
• Title:         {pr_info['title']}
• State:         {pr_info['state'].upper()} (Open)
• Author:        {pr_info['user']['login']}
• Created:       {datetime.fromisoformat(pr_info['created_at'].replace('Z', '+00:00')).strftime('%Y-%m-%d %H:%M:%S')}
• Source branch: {pr_info['head']['ref']}
• Target branch: {pr_info['base']['ref']}

📝 DESCRIPTION:
{pr_info['body'] or 'No description provided'}

📊 STATISTICS:
• Commits:          {len(commits)}
• Files modified:   {len(files)}
• Additions:        +{pr_info.get('additions', 0)} lines
• Deletions:        -{pr_info.get('deletions', 0)} lines
• Reviews:          {len(reviews)}
• Code comments:    {len(comments)}
• General comments: {len(issue_comments)}

"""

    if reviews:
        analysis += "\n" + "="*60 + "\n👥 PR REVIEWS:\n" + "="*60 + "\n\n"
        for i, review in enumerate(reviews, 1):
            review_date = datetime.fromisoformat(
                review["submitted_at"].replace("Z", "+00:00")
            ).strftime("%Y-%m-%d %H:%M")
            state_emoji = {
                "APPROVED": "✅", "CHANGES_REQUESTED": "❌",
                "COMMENTED": "💬", "DISMISSED": "🚫"
            }.get(review["state"], "❓")
            analysis += f"{i}. {state_emoji} {review['state']} by {review['user']['login']}\n"
            analysis += f"   📅 {review_date}\n"
            if review.get("body"):
                for line in review["body"].split("\n"):
                    analysis += f"      {line}\n"
            analysis += "\n"

    if comments:
        analysis += "\n" + "="*60 + f"\n💻 CODE REVIEW COMMENTS ({len(comments)}):\n" + "="*60 + "\n\n"
        comments_by_file = {}
        for c in comments:
            fp = c.get("path", "Unknown file")
            comments_by_file.setdefault(fp, []).append(c)
        for fp, fc in comments_by_file.items():
            analysis += f"📄 {fp} ({len(fc)} comments):\n" + "-"*50 + "\n"
            for c in fc:
                c_date = datetime.fromisoformat(
                    c["created_at"].replace("Z", "+00:00")
                ).strftime("%Y-%m-%d %H:%M")
                line = c.get("line", c.get("original_line", "N/A"))
                analysis += f"   👤 {c['user']['login']} at line {line}\n   📅 {c_date}\n"
                if c.get("diff_hunk"):
                    for hl in c["diff_hunk"].split("\n")[:3]:
                        analysis += f"      {hl}\n"
                if c.get("body"):
                    for bl in c["body"].split("\n"):
                        analysis += f"      {bl}\n"
                analysis += "\n"

    if issue_comments:
        analysis += "\n" + "="*60 + f"\n💬 GENERAL DISCUSSION ({len(issue_comments)}):\n" + "="*60 + "\n\n"
        for i, c in enumerate(issue_comments, 1):
            c_date = datetime.fromisoformat(
                c["created_at"].replace("Z", "+00:00")
            ).strftime("%Y-%m-%d %H:%M")
            analysis += f"{i}. 👤 {c['user']['login']}\n   📅 {c_date}\n"
            if c.get("body"):
                for line in c["body"].split("\n"):
                    analysis += f"      {line}\n"
            analysis += "\n"

    if commits:
        analysis += "\n" + "="*60 + "\n🔄 COMMITS:\n" + "="*60 + "\n\n"
        for i, commit in enumerate(commits, 1):
            c_date = datetime.fromisoformat(
                commit["commit"]["author"]["date"].replace("Z", "+00:00")
            ).strftime("%Y-%m-%d %H:%M")
            msg = commit["commit"]["message"].split("\n")[0]
            analysis += f"{i:2d}. [{commit['sha'][:8]}] {msg}\n"
            analysis += f"     👤 {commit['commit']['author']['name']} — {c_date}\n"

    if files:
        analysis += f"\n" + "="*60 + f"\n📁 MODIFIED FILES ({len(files)} total):\n" + "="*60 + "\n\n"
        for f in files:
            emoji = {"added": "✅", "modified": "📝", "removed": "❌",
                     "renamed": "🔄", "copied": "📋"}.get(f["status"], "❓")
            analysis += f"{emoji} {f['filename']} (+{f['additions']}/-{f['deletions']})\n"
            if f.get("patch"):
                analysis += f"   📄 DIFF: {len(f['patch'].split(chr(10)))} lines\n"
                analysis += f"   🔗 RAW: {f.get('raw_url', 'N/A')}\n"

    return analysis


# ─────────────────────────────────────────────
# REPOSITORY TRAVERSAL
# ─────────────────────────────────────────────

def get_contents(repo_name, path, branch):
    url = f"https://api.github.com/repos/{REPO_OWNER}/{repo_name}/contents/{path}?ref={branch}"
    return make_request(url) or []


def is_important_file(filename):
    _, ext = os.path.splitext(filename.lower())
    return ext in IMPORTANT_EXTENSIONS


def should_include_path(path, current_depth):
    if current_depth == 0:
        return True
    parts = path.split("/")
    return parts[0] in IMPORTANT_FOLDERS if parts else False


def traverse_repository(repo_name, path, branch, indent="", max_depth=5, current_depth=0):
    if current_depth > max_depth:
        return "", []

    items = get_contents(repo_name, path, branch)
    if not items:
        return "", []
    if isinstance(items, dict) and items.get("type") == "file":
        items = [items]

    tree = ""
    raw_links = []
    dirs  = [i for i in items if i.get("type") == "dir"]
    files = [i for i in items if i.get("type") == "file"]

    for d in dirs:
        dir_name = d["name"]
        if (current_depth == 0 and dir_name in IMPORTANT_FOLDERS) or \
           (current_depth > 0 and should_include_path(d["path"], current_depth)):
            tree += f"{indent}📁 {dir_name}/\n"
            subtree, sublinks = traverse_repository(
                repo_name, d["path"], branch, indent + "    ", max_depth, current_depth + 1
            )
            tree += subtree
            raw_links.extend(sublinks)

    for f in files:
        filename = f["name"]
        file_path = f["path"]
        if current_depth == 0:
            if filename.lower() in ROOT_FILES_TO_INCLUDE:
                size = f.get("size", 0)
                tree += f"{indent}├─ {filename} ({size} bytes)\n{indent}   📄 RAW: {f['download_url']}\n"
                raw_links.append({"filename": filename, "path": file_path,
                                   "url": f["download_url"], "size": size})
        else:
            if should_include_path(file_path, current_depth) and is_important_file(filename):
                size = f.get("size", 0)
                tree += f"{indent}├─ {filename} ({size} bytes)\n{indent}   📄 RAW: {f['download_url']}\n"
                raw_links.append({"filename": filename, "path": file_path,
                                   "url": f["download_url"], "size": size})

    return tree, raw_links


# ─────────────────────────────────────────────
# GIST CREATION
# ─────────────────────────────────────────────

def create_gist(title, content, description="Repository analysis"):
    gist_url = "https://api.github.com/gists"
    if len(content) > 900_000:
        content = content[:900_000] + "\n\n... [TRUNCATED — FILE TOO LARGE] ..."
    gist_data = {
        "description": description,
        "public": True,
        "files": {f"{title}.txt": {"content": content}}
    }
    try:
        response = requests.post(gist_url, headers=HEADERS, json=gist_data)
        if response.status_code == 201:
            info = response.json()
            return {
                "url": info["html_url"],
                "raw_url": list(info["files"].values())[0]["raw_url"],
                "id": info["id"]
            }
        else:
            msg = response.json().get("message", "Unknown error")
            print(f"❌ Error creating Gist ({response.status_code}): {msg}")
            if response.status_code == 403:
                print("💡 Token may be missing 'gist' scope — check your GitHub token settings.")
            return None
    except Exception as e:
        print(f"❌ Gist creation failed: {e}")
        return None


def create_multi_file_gist(title, files_content, description="Multi-file analysis"):
    gist_url = "https://api.github.com/gists"
    gist_files = {}
    total = 0
    for filename, content in files_content.items():
        if len(content) > 300_000:
            content = content[:300_000] + "\n\n... [TRUNCATED] ..."
        total += len(content)
        if total > 900_000:
            break
        safe = filename.replace("/", "_").replace("\\", "_").replace(" ", "_")
        gist_files[safe] = {"content": content}
    gist_data = {"description": description, "public": True, "files": gist_files}
    try:
        response = requests.post(gist_url, headers=HEADERS, json=gist_data)
        if response.status_code == 201:
            info = response.json()
            return {
                "url": info["html_url"],
                "id": info["id"],
                "files": {name: f["raw_url"] for name, f in info["files"].items()}
            }
        else:
            print(f"❌ Error creating Gist: {response.status_code}")
            return None
    except Exception as e:
        print(f"❌ Gist creation failed: {e}")
        return None


# ─────────────────────────────────────────────
# ANALYSIS GENERATOR
# ─────────────────────────────────────────────

def generate_comprehensive_analysis(repo_name, branch_name=None, pr_number=None):
    analysis_parts = {}

    if branch_name:
        print(f"\n📂 Traversing branch '{branch_name}' (lib, i18n, test + pubspec)...")
        tree, raw_links = traverse_repository(repo_name, "", branch_name)
        structure = f"""REPOSITORY ANALYSIS — BRANCH: {branch_name}
REPO: {REPO_OWNER}/{repo_name}
FOLDERS SCANNED: lib, i18n, test + pubspec.yml
{'='*80}

📁 STRUCTURE:
{'='*40}
{tree}

📄 IMPORTANT FILES ({len(raw_links)}):
{'='*40}
"""
        for lnk in raw_links:
            structure += f"📋 {lnk['path']}\n   🔗 {lnk['url']}\n   📏 {lnk['size']} bytes\n\n"
        analysis_parts[f"01_branch_{branch_name.replace('/', '_')}"] = structure

        for key_file in ["pubspec.yaml", "pubspec.yml"]:
            data = get_contents(repo_name, key_file, branch_name)
            if data and isinstance(data, dict) and data.get("type") == "file":
                try:
                    decoded = base64.b64decode(data["content"]).decode("utf-8")
                    analysis_parts[f"02_{key_file}"] = f"CONTENT: {key_file}\n{'='*50}\n{decoded}\n"
                except Exception:
                    analysis_parts[f"02_{key_file}"] = f"❌ Could not decode {key_file}"
                break

    if pr_number:
        print(f"\n🔍 Fetching details for PR #{pr_number}...")
        pr_details = get_pr_details(repo_name, pr_number)
        if pr_details["pr_info"]:
            analysis_parts[f"03_pr_{pr_number}"] = format_pr_analysis(pr_details)

            files = pr_details["files"] or []
            if files:
                diff_content = f"""DIFFS — PR #{pr_number} (ALL FILES)
{'='*50}\n\n"""
                for f in files:
                    diff_content += f"\n📄 {f['filename']}\n   Status: {f['status']} (+{f['additions']}/-{f['deletions']})\n"
                    diff_content += f"   Raw: {f.get('raw_url', 'N/A')}\n\nDIFF:\n{'-'*40}\n"
                    diff_content += f.get("patch", "⚠️  No diff (binary or too large)")
                    diff_content += f"\n{'-'*40}\n"
                    diff_content += f"🔗 FULL FILE: {f.get('raw_url', 'N/A')}\n"
                analysis_parts[f"04_diffs_pr_{pr_number}"] = diff_content

    return analysis_parts


# ─────────────────────────────────────────────
# ✅ MAIN FLOW (updated)
# ─────────────────────────────────────────────

def auto_gist_analysis():
    print("\n🚀 GITHUB ANALYZER — develop4God")
    print("="*50)

    # ── STEP 1: Select repo ──────────────────
    repo_name = select_repo_interactively(REPO_OWNER)
    if not repo_name:
        return

    # ── STEP 2: Choose analysis type ─────────
    print("\nWhat type of analysis do you want?")
    print("1. 📂 Specific branch")
    print("2. 🔍 Open Pull Request")
    print("3. 🎯 Branch + PR (complete)")

    choice = input("Select (1-3): ").strip()

    branch_name = None
    pr_number   = None

    # ── STEP 3: Branch selection ──────────────
    if choice in ("1", "3"):
        branches = get_branches(repo_name)
        if not branches:
            print(f"❌ No branches found for '{repo_name}'")
            return

        print(f"\n🌿 Branches in '{repo_name}':")
        for i, b in enumerate(branches, 1):
            print(f"  {i:3d}. {b}")
        try:
            sel = int(input("Select branch number: ").strip()) - 1
            branch_name = branches[sel]
            print(f"✅ Branch: {branch_name}")
        except (ValueError, IndexError):
            print("❌ Invalid selection")
            return

    # ── STEP 4: PR selection (OPEN ONLY) ─────
    if choice in ("2", "3"):
        if choice == "3" and branch_name:
            # Show open PRs for this specific branch first
            branch_prs = display_branch_open_prs(repo_name, branch_name)
            if branch_prs:
                use_branch_pr = input("Select from branch PRs above? (y/n): ").strip().lower()
                if use_branch_pr == "y":
                    try:
                        idx = int(input(f"PR number (1-{len(branch_prs)}): ").strip()) - 1
                        pr_number = branch_prs[idx]["number"]
                        print(f"✅ Selected PR #{pr_number}")
                    except (ValueError, IndexError):
                        print("❌ Invalid, continuing without PR")
                    # done — skip the general listing below
                    branch_prs = None  # flag to skip
                else:
                    branch_prs = None  # user chose to see all PRs
            if branch_prs is not None:
                pass  # already selected
            elif pr_number is None:
                # Fall through to general open PR listing
                open_prs = display_open_prs_numbered(repo_name)
                if not open_prs:
                    print("ℹ️  No open PRs available — continuing with branch-only analysis.")
                else:
                    try:
                        idx = int(input("Select PR number: ").strip()) - 1
                        pr_number = open_prs[idx]["number"]
                        print(f"✅ Selected PR #{pr_number}")
                    except (ValueError, IndexError):
                        print("❌ Invalid, continuing without PR")
        else:
            # PR only
            open_prs = display_open_prs_numbered(repo_name)
            if not open_prs:
                print("⚠️  No open PRs found. Nothing to analyze.")
                return
            try:
                idx = int(input("Select PR number: ").strip()) - 1
                pr_number = open_prs[idx]["number"]
                print(f"✅ Selected PR #{pr_number}")
            except (ValueError, IndexError):
                print("❌ Invalid selection")
                return

    # ── STEP 5: Generate analysis ─────────────
    print("\n📡 Generating analysis...")
    analysis_parts = generate_comprehensive_analysis(repo_name, branch_name, pr_number)

    if not analysis_parts:
        print("❌ Nothing was generated — check selections and try again.")
        return

    # ── STEP 6: Upload to Gist ────────────────
    parts = []
    if branch_name:
        parts.append(f"branch-{branch_name.replace('/', '-')}")
    if pr_number:
        parts.append(f"PR-{pr_number}")

    title = f"{repo_name}_{'_'.join(parts)}"
    desc  = f"Analysis of {REPO_OWNER}/{repo_name}"
    if branch_name:
        desc += f" | branch: {branch_name}"
    if pr_number:
        desc += f" | PR #{pr_number} (open)"

    print("📡 Uploading to GitHub Gist...")
    if len(analysis_parts) == 1:
        gist_result = create_gist(title, list(analysis_parts.values())[0], desc)
    else:
        gist_result = create_multi_file_gist(title, analysis_parts, desc)

    if gist_result:
        print("\n✅ GIST CREATED SUCCESSFULLY!")
        print("="*50)
        print(f"🔗 Gist URL: {gist_result['url']}")
        if "files" in gist_result:
            print(f"\n📁 Files ({len(gist_result['files'])}):")
            for fname, raw in gist_result["files"].items():
                print(f"   📄 {fname}: {raw}")
        elif "raw_url" in gist_result:
            print(f"📄 Raw: {gist_result['raw_url']}")
        print(f"\n💡 Paste this URL into Claude:\n   {gist_result['url']}")

        if input("\n🌐 Open in browser? (y/n): ").strip().lower() == "y":
            try:
                import webbrowser
                webbrowser.open(gist_result["url"])
                print("✅ Browser opened!")
            except Exception as e:
                print(f"❌ Could not open browser: {e}")
    else:
        print("\n❌ Gist creation failed.")
        print("💡 Ensure your token has the 'gist' scope enabled.")


if __name__ == "__main__":
    auto_gist_analysis()

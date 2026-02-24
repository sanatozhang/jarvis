"""
Linear API client.

Handles:
- Issue fetching via GraphQL API
- Attachment downloading
- Comment creation (posting analysis results)
- Webhook signature verification
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlparse

import httpx

from app.config import get_settings

logger = logging.getLogger("jarvis.linear")

GRAPHQL_URL = "https://api.linear.app/graphql"


class LinearClient:
    """Async Linear GraphQL API client."""

    def __init__(self):
        settings = get_settings()
        self._api_key = settings.linear.api_key
        self._http = httpx.AsyncClient(timeout=60)

    @property
    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": self._api_key,
            "Content-Type": "application/json",
        }

    async def _graphql(self, query: str, variables: Optional[Dict] = None) -> Dict[str, Any]:
        """Execute a GraphQL query against the Linear API."""
        payload: Dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables

        resp = await self._http.post(GRAPHQL_URL, json=payload, headers=self._headers)
        resp.raise_for_status()
        result = resp.json()

        if result.get("errors"):
            errors = result["errors"]
            logger.error("Linear GraphQL errors: %s", errors)
            raise RuntimeError(f"Linear API error: {errors[0].get('message', errors)}")

        return result.get("data", {})

    # ------------------------------------------------------------------
    # Issue operations
    # ------------------------------------------------------------------
    async def get_issue(self, issue_id: str) -> Dict[str, Any]:
        """Fetch an issue by its UUID."""
        query = """
        query GetIssue($id: String!) {
            issue(id: $id) {
                id
                identifier
                title
                description
                url
                priority
                state { name }
                assignee { name email }
                labels { nodes { name } }
                attachments { nodes { id title url metadata } }
                createdAt
            }
        }
        """
        data = await self._graphql(query, {"id": issue_id})
        return data.get("issue", {})

    async def get_comment(self, comment_id: str) -> Dict[str, Any]:
        """Fetch a comment by its UUID."""
        query = """
        query GetComment($id: String!) {
            comment(id: $id) {
                id
                body
                issue { id identifier title url }
                user { id name isMe }
                createdAt
            }
        }
        """
        data = await self._graphql(query, {"id": comment_id})
        return data.get("comment", {})

    async def get_issue_comments(self, issue_id: str) -> List[Dict[str, Any]]:
        """Fetch all comments for an issue."""
        query = """
        query GetIssueComments($id: String!) {
            issue(id: $id) {
                comments {
                    nodes {
                        id
                        body
                        user { id name }
                        createdAt
                    }
                }
            }
        }
        """
        data = await self._graphql(query, {"id": issue_id})
        issue = data.get("issue", {})
        return issue.get("comments", {}).get("nodes", [])

    async def collect_uploaded_files(self, issue_id: str, description: str = "") -> List[Dict[str, str]]:
        """
        Collect all uploaded file URLs from an issue's description and comments.

        Linear stores uploaded files on its CDN. They appear as markdown links
        in description/comments. We extract all non-image URLs to be safe.

        Returns list of {"url": ..., "filename": ..., "source": "description"|"comment"}
        """
        files: List[Dict[str, str]] = []
        seen_urls: set[str] = set()

        # Log raw content for debugging
        logger.info("=== Scanning issue %s for uploaded files ===", issue_id)
        logger.info("  Description length: %d chars", len(description or ""))
        if description:
            logger.debug("  Description content:\n%s", description[:2000])

        # Extract from description
        all_urls_desc = _extract_all_urls(description)
        logger.info("  URLs found in description: %d", len(all_urls_desc))
        for url, filename, url_type in all_urls_desc:
            logger.info("    [%s] %s → %s", url_type, filename, url[:120])

        for url, filename, _ in all_urls_desc:
            if url not in seen_urls and not _is_image(filename):
                files.append({"url": url, "filename": filename, "source": "description"})
                seen_urls.add(url)

        # Extract from comments
        try:
            comments = await self.get_issue_comments(issue_id)
            logger.info("  Comments found: %d", len(comments))
            for i, comment in enumerate(comments):
                body = comment.get("body", "") or ""
                user = comment.get("user", {}).get("name", "?")
                logger.info("  Comment #%d by %s (%d chars)", i, user, len(body))
                if body:
                    logger.debug("    Content:\n%s", body[:1000])

                all_urls_comment = _extract_all_urls(body)
                for url, filename, url_type in all_urls_comment:
                    logger.info("    [%s] %s → %s", url_type, filename, url[:120])
                    if url not in seen_urls and not _is_image(filename):
                        files.append({"url": url, "filename": filename, "source": "comment"})
                        seen_urls.add(url)
        except Exception as e:
            logger.warning("Failed to fetch comments for file extraction: %s", e)

        logger.info("=== Result: %d downloadable files found ===", len(files))
        for f in files:
            logger.info("  ✓ [%s] %s", f["source"], f["filename"])

        return files

    # ------------------------------------------------------------------
    # Comment operations
    # ------------------------------------------------------------------
    async def create_comment(self, issue_id: str, body: str) -> Dict[str, Any]:
        """Create a comment on an issue."""
        mutation = """
        mutation CreateComment($issueId: String!, $body: String!) {
            commentCreate(input: { issueId: $issueId, body: $body }) {
                success
                comment {
                    id
                    body
                    createdAt
                }
            }
        }
        """
        data = await self._graphql(mutation, {"issueId": issue_id, "body": body})
        result = data.get("commentCreate", {})
        if not result.get("success"):
            raise RuntimeError(f"Failed to create comment on issue {issue_id}")
        logger.info("Comment created on Linear issue %s", issue_id)
        return result.get("comment", {})

    # ------------------------------------------------------------------
    # Label operations (optional: mark issue as "AI分析中")
    # ------------------------------------------------------------------
    async def add_label_by_name(self, issue_id: str, label_name: str) -> bool:
        """Add a label to an issue by label name. Returns True on success."""
        # First, find the label ID
        query = """
        query FindLabel($name: String!) {
            issueLabels(filter: { name: { eq: $name } }) {
                nodes { id name }
            }
        }
        """
        data = await self._graphql(query, {"name": label_name})
        labels = data.get("issueLabels", {}).get("nodes", [])
        if not labels:
            logger.warning("Label '%s' not found in Linear", label_name)
            return False

        label_id = labels[0]["id"]

        # Get current label IDs
        issue = await self.get_issue(issue_id)
        current_labels = [l["name"] for l in issue.get("labels", {}).get("nodes", [])]
        if label_name in current_labels:
            return True  # already has the label

        # Add label via issue update
        mutation = """
        mutation AddLabel($issueId: String!, $labelIds: [String!]!) {
            issueUpdate(id: $issueId, input: { labelIds: $labelIds }) {
                success
            }
        }
        """
        current_label_ids = [l.get("id", "") for l in issue.get("labels", {}).get("nodes", []) if l.get("id")]
        current_label_ids.append(label_id)
        data = await self._graphql(mutation, {"issueId": issue_id, "labelIds": current_label_ids})
        return data.get("issueUpdate", {}).get("success", False)

    # ------------------------------------------------------------------
    # File download
    # ------------------------------------------------------------------
    async def download_attachment(self, url: str, save_path: str) -> str:
        """Download an attachment file from URL and save to disk."""
        import aiofiles

        logger.info("[download] Requesting: %s", url[:150])

        # Linear uploaded files require API key auth to download
        headers = {"Authorization": self._api_key}
        resp = await self._http.get(url, headers=headers, follow_redirects=True)
        resp.raise_for_status()

        content_type = resp.headers.get("content-type", "")
        content_length = resp.headers.get("content-length", "?")
        logger.info("[download] Response: %d %s | content-type: %s | content-length: %s",
                     resp.status_code, resp.reason_phrase, content_type, content_length)

        # Try to get the real filename from Content-Disposition header
        cd = resp.headers.get("content-disposition", "")
        if cd:
            logger.info("[download] Content-Disposition: %s", cd)
            import cgi
            _, params = cgi.parse_header(cd)
            real_name = params.get("filename", "")
            if real_name and "." in real_name:
                save_path = str(Path(save_path).parent / real_name)
                logger.info("[download] ✓ Real filename from header: %s", real_name)
        else:
            logger.info("[download] No Content-Disposition header (filename unknown)")

        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(save_path, "wb") as f:
            await f.write(resp.content)

        final_size = Path(save_path).stat().st_size
        magic = b""
        with open(save_path, "rb") as f:
            magic = f.read(8)
        logger.info("[download] ✓ Saved: %s (%d bytes, magic: %s)",
                     Path(save_path).name, final_size, magic.hex() if magic else "empty")
        return save_path

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------
    async def close(self):
        await self._http.aclose()


# ---------------------------------------------------------------------------
# Extract URLs from markdown text
# ---------------------------------------------------------------------------
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico"}

# URLs to ignore (not files)
_IGNORE_DOMAINS = {"linear.app", "github.com", "twitter.com", "x.com", "google.com"}


def _extract_all_urls(text: str) -> List[tuple[str, str, str]]:
    """
    Extract ALL URLs from markdown text, as permissively as possible.

    Returns list of (url, filename, type) tuples.
    type is "markdown_link", "markdown_image", or "raw_url".
    """
    if not text:
        return []

    results: List[tuple[str, str, str]] = []
    seen: set[str] = set()

    # Pattern 1: Markdown links [text](url)
    for match in re.finditer(r'\[([^\]]*)\]\((https?://[^)]+)\)', text):
        label = match.group(1)
        url = match.group(2).strip()
        filename = _filename_from_url(url) or label or "attachment"
        if url not in seen:
            results.append((url, filename, "markdown_link"))
            seen.add(url)

    # Pattern 2: Markdown images ![text](url)
    for match in re.finditer(r'!\[([^\]]*)\]\((https?://[^)]+)\)', text):
        label = match.group(1)
        url = match.group(2).strip()
        filename = _filename_from_url(url) or label or "image"
        if url not in seen:
            results.append((url, filename, "markdown_image"))
            seen.add(url)

    # Pattern 3: Raw URLs (not already captured by markdown patterns)
    for match in re.finditer(r'(https?://[^\s<>\]\)\"]+)', text):
        url = match.group(1).strip().rstrip(".,;:)")
        if url not in seen:
            filename = _filename_from_url(url) or "attachment"
            results.append((url, filename, "raw_url"))
            seen.add(url)

    return results


def _filename_from_url(url: str) -> str:
    """Extract a clean filename from a URL."""
    try:
        parsed = urlparse(url)
        path = unquote(parsed.path)
        if "/" in path:
            name = path.rsplit("/", 1)[-1]
            if name:
                return name
    except Exception:
        pass
    return ""


def _is_image(filename: str) -> bool:
    """Check if a filename looks like an image."""
    ext = Path(filename).suffix.lower() if filename else ""
    return ext in _IMAGE_EXTENSIONS


# ---------------------------------------------------------------------------
# Webhook signature verification
# ---------------------------------------------------------------------------
def verify_webhook_signature(body: bytes, signature: str, secret: str) -> bool:
    """
    Verify Linear webhook signature.

    Linear signs webhooks with HMAC SHA-256:
      signature = HMAC-SHA256(secret, body)
    The signature is sent in the `Linear-Signature` header.
    """
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------------------
# Format analysis result as markdown comment
# ---------------------------------------------------------------------------
def format_analysis_comment(result: Dict[str, Any], issue_identifier: str = "") -> str:
    """Format an AnalysisResult dict as a markdown comment for Linear."""
    confidence = result.get("confidence", "medium")
    confidence_emoji = {"high": "✅", "medium": "⚠️", "low": "❌"}.get(confidence, "")

    lines = [
        "## AI Analysis Result",
        "",
        f"**Problem Type**: {result.get('problem_type', 'Unknown')}",
        f"**Confidence**: {confidence} {confidence_emoji}",
        f"**Root Cause**: {result.get('root_cause', '')}",
    ]

    if result.get("confidence_reason"):
        lines.append(f"**Confidence Reason**: {result['confidence_reason']}")

    evidence = result.get("key_evidence", [])
    if evidence:
        lines.append("")
        lines.append("### Key Evidence")
        for e in evidence[:5]:
            lines.append(f"- `{e}`")

    user_reply = result.get("user_reply", "")
    if user_reply:
        lines.append("")
        lines.append("### Suggested Reply")
        lines.append(f"> {user_reply.replace(chr(10), chr(10) + '> ')}")

    if result.get("needs_engineer"):
        lines.append("")
        lines.append("**⚠️ Needs engineer review**")

    if result.get("fix_suggestion"):
        lines.append("")
        lines.append("### Fix Suggestion")
        lines.append(result["fix_suggestion"])

    # Footer
    agent = result.get("agent_type", "")
    rule = result.get("rule_type", "")
    footer_parts = ["Analyzed by Jarvis AI"]
    if agent:
        footer_parts.append(f"Agent: {agent}")
    if rule:
        footer_parts.append(f"Rule: {rule}")
    lines.append("")
    lines.append(f"---\n*{' | '.join(footer_parts)}*")

    return "\n".join(lines)

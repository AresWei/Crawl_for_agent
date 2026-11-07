"""Inject source filters' head requests into the current job's HTTP owner."""
from contextvars import ContextVar
from bs4 import BeautifulSoup

head_fetcher = ContextVar("web_head_fetcher", default=None)


class HeadPeekr:
    @staticmethod
    async def peek_html(url, timeout=.3):
        fetch = head_fetcher.get()
        if fetch is None:
            raise RuntimeError("Head filters require a job-scoped transport")
        document = await fetch(url)
        return document.split("</head>", 1)[0] + "</head>"

    @staticmethod
    def extract_meta_tags(document):
        soup = BeautifulSoup(document, "lxml")
        result = {str(n.get("name") or n.get("property")): n.get("content", "")
                  for n in soup.find_all("meta") if n.get("name") or n.get("property")}
        canonical = soup.select_one('link[rel="canonical"]')
        if canonical:
            result["canonical"] = canonical.get("href", "")
        return result

    @staticmethod
    def get_title(document):
        soup = BeautifulSoup(document, "lxml")
        return soup.title.get_text(" ", strip=True) if soup.title else None

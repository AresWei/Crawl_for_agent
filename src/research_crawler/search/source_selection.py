"""Bounded lexical source selection; a relevance heuristic, never entailment proof."""
import re
import math
from urllib.parse import urlsplit, urlunsplit

STOP = set('a an the is are was were in on at by to of and or for with how what which can does do be it this that please explain according source sources'.split())
ZH_STOP = {'什么','如何','是否','可以','这个','哪些','请问','根据','来源','回答','说明','一个','进行',
           '是','的','了','在','它','其','与','和','及','中','用','请','实际','读取'}
LINK = re.compile(r'!?\[([^\]]*)\]\([^\n]*?\)')


def query_terms(query):
    tokens = re.findall(r'[a-zA-Z0-9]+', query.lower())
    if re.search(r'[\u4e00-\u9fff]', query):
        import jieba
        tokens.extend(jieba.cut(' '.join(re.findall(r'[\u4e00-\u9fff]+', query))))
    return list(dict.fromkeys(t for t in tokens if t.strip() and t not in STOP and t not in ZH_STOP))[:40]


def matches(text, terms):
    lower = text.lower()
    return [t for t in terms if (t in lower if re.search(r'[\u4e00-\u9fff]',t)
            else re.search(r'(?<![a-z0-9])'+re.escape(t[:-1] if len(t)>3 and t.endswith('s') and not t.endswith('ss') else t)+r's?(?![a-z0-9])',lower))]


def relevance(text, query):
    """Discovery-only diagnostic; no claim that a snippet proves an answer."""
    terms = query_terms(query)
    matched = matches(text, terms)
    minimum = max(1, (len(terms)+1)//2)
    return {'matched_terms': matched, 'query_terms': terms, 'required_terms': minimum,
            'eligible': bool(terms) and len(matched) >= minimum,
            'semantic_support_proven': False}


def _prose_mask(text):
    """Same-length scoring view; ranges always address untouched text.

    Classify before windowing, so a slice inside a code fence stays masked.
    Keep anchors in explanatory sentences, omit standalone menus, pipe tables,
    fenced/indented examples and URL destinations from relevance scores.
    """
    chunks = []
    fence = None
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        marker = re.match(r'(`{3,}|~{3,})', stripped)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            chunks.append(' ' * len(line))
            continue
        if fence or line.startswith(('    ', '\t')) or line.count('|') >= 2:
            chunks.append(' ' * len(line))
            continue
        links = list(LINK.finditer(line))
        remaining = LINK.sub('', line)
        if links and len(re.sub(r'[\W_\d]+', '', remaining)) < 12:
            chunks.append(' ' * len(line))
            continue
        masked = LINK.sub(lambda m: m.group(1) + ' ' * (len(m.group(0))-len(m.group(1))), line)
        masked = re.sub(r'https?://\S+', lambda m: ' '*len(m.group(0)), masked)
        chunks.append(masked)
    return ''.join(chunks)


def select_excerpt(payload, query, question=None, *, window_chars=1400):
    if type(window_chars) is not int or not 400 <= window_chars <= 2200:
        raise ValueError('window_chars must be an integer 400..2200')
    terms = query_terms(query)
    question_terms = query_terms(question) if question else []
    best = None
    for field in ('markdown','fit_markdown'):
        text = payload.get(field,'')
        if not isinstance(text,str) or not text.strip():
            continue
        prose = _prose_mask(text)
        stride = max(window_chars//2,(len(text)+2047)//2048)
        starts = set(range(0,len(text),stride))
        boundaries = [m.end() for m in re.finditer(r'\n\s*\n|\n(?=#{1,6} )',text)]
        step = max(1,(len(boundaries)+2047)//2048)
        starts.update(boundaries[::step])
        for start in sorted(starts):
            excerpt = text[start:start+window_chars]
            body = prose[start:start+window_chars]
            matched = matches(body,terms)
            question_matched = matches(body,question_terms)
            body_chars = len(re.sub(r'\s','',body))
            density = body_chars/max(1,len(re.sub(r'\s','',excerpt)))
            score = len(matched) + .65*len(set(question_matched)-set(terms)) + .8*density + .1*min(1,body_chars/600)
            candidate = (score,-start,field,excerpt,start,matched,question_matched,density,body_chars)
            if best is None or candidate>best:
                best = candidate
    if best is None:
        return 'markdown','',{'start':0,'end':0,'unit':'unicode_code_points','end_exclusive':True}, {'eligible':False,'reason':'insufficient_page_text'}
    _,_,field,excerpt,start,matched,question_matched,density,body_chars = best
    minimum = max(1, (len(terms)+1)//2)
    eligible = bool(terms) and body_chars>=40 and len(matched)>=minimum
    return field,excerpt,{'start':start,'end':start+len(excerpt),'unit':'unicode_code_points','end_exclusive':True}, {
        'eligible':eligible,'reason':'lexical_match' if eligible else 'low_query_relevance',
        'matched_terms':matched,'required_terms':minimum,'query_terms':terms,
        'question_matched_terms':question_matched,'question_is_ranking_only':True,
        'strategy':'prose_question_window_v2','window_chars':window_chars,'prose_density':round(density,4),
        'semantic_support_proven':False}


def follow_candidates(payload, query, parent_url, question=None, *, allow_external=False):
    terms = query_terms(query)
    extra = [t for t in query_terms(question or '') if t not in terms]
    host = urlsplit(parent_url).hostname
    candidates = []
    seen = set()
    for link in payload.get('links',[]):
        url = link.get('url','')
        try:
            parts = urlsplit(url)
            if (parts.scheme not in {'http','https'} or not parts.hostname or parts.username or parts.password
                    or (parts.hostname != host and not allow_external)):
                continue
        except (TypeError,ValueError):
            continue
        url = urlunsplit((parts.scheme,parts.netloc,parts.path or '/',parts.query,''))
        if url in seen:
            continue
        text = link.get('text','')+' '+parts.path
        matched = matches(text,terms)
        if matched:
            candidates.append((url,matched,matches(text,extra)))
            seen.add(url)
    frequencies = {term:sum(term in matched for _,matched,_ in candidates) for term in terms}
    ranked = [(-sum(1+math.log((len(candidates)+1)/(frequencies[term]+1)) for term in matched)-.65*len(extra_matched),index,url)
              for index,(url,matched,extra_matched) in enumerate(candidates)]
    return [url for _,_,url in sorted(ranked)]

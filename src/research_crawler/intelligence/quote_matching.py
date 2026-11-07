"""Literal quotation matching across a narrow Markdown display projection.

No semantic matching, case folding, punctuation deletion, escape decoding or
Unicode substitution. Returned quotations always come from one original span.
"""
import re

PROJECTION_VERSION = 'quote-markdown-display-v1'


def _span(start, end):
    return {'start':start, 'end':end, 'unit':'unicode_code_points', 'end_exclusive':True}


def _project(text):
    keep=bytearray(b'\1')*len(text)
    protected=bytearray(len(text))
    wrappers=[]
    # Code and escaped characters cannot acquire formatting semantics later.
    fence=None;offset=0
    for line in text.splitlines(keepends=True):
        marker=re.match(r'^\s{0,3}(`{3,}|~{3,})',line)
        if marker:
            token=marker.group(1)
            if fence is None:fence=token
            elif token[0]==fence[0] and len(token)>=len(fence):fence=None
            protected[offset:offset+len(line)]=b'\1'*len(line)
        elif fence or line.startswith(('    ','\t')):
            protected[offset:offset+len(line)]=b'\1'*len(line)
        offset+=len(line)
    cursor=0
    while cursor<len(text):
        if text[cursor]=='\\':
            protected[cursor:min(len(text),cursor+2)]=b'\1'*min(2,len(text)-cursor)
            cursor+=2
        else:cursor+=1
    for match in re.finditer(r'(?<!`)(`{1,2})(?!`)([^\n]+?)\1(?!`)',text):
        if protected[match.start()]:continue
        left,right=match.span(2)
        protected[match.start():match.end()]=b'\1'*(match.end()-match.start())
        keep[match.start():left]=b'\0'*(left-match.start())
        keep[right:match.end()]=b'\0'*(match.end()-right)
        wrappers.append((match.start(),left,right,match.end()))
    # Only inline links with a complete, balanced destination are transformed.
    for match in re.finditer(r'(?<![!\\])\[([^\[\]\n]+)\]\(',text):
        if protected[match.start()] or not keep[match.start()]:continue
        cursor=match.end();depth=1;quote=None
        while cursor<len(text) and cursor-match.end()<16384:
            char=text[cursor]
            if char=='\\':cursor+=2;continue
            if quote:
                if char==quote:quote=None
            elif char in {'"',"'"} and cursor>match.end() and text[cursor-1].isspace():quote=char
            elif char=='(':depth+=1
            elif char==')':
                depth-=1
                if depth==0:break
            elif char=='\n':break
            cursor+=1
        if depth:continue
        left,right=match.span(1)
        keep[match.start():left]=b'\0'*(left-match.start())
        keep[right:cursor+1]=b'\0'*(cursor+1-right)
        wrappers.append((match.start(),left,right,cursor+1))
    # Conservative flanking rules avoid turning multiplication/in-word stars
    # into emphasis. Ambiguous/nested or escaped markers remain literal.
    for match in re.finditer(r'(?<![\w\\*])(\*\*|\*)(?!\*)(\S(?:[^*\n]*?\S)?)\1(?![\w*])',text):
        left,right=match.span(2)
        if (any(protected[match.start():left]) or any(protected[right:match.end()])
                or not all(keep[match.start():left]) or not all(keep[right:match.end()])):
            continue
        keep[match.start():left]=b'\0'*(left-match.start())
        keep[right:match.end()]=b'\0'*(match.end()-right)
        wrappers.append((match.start(),left,right,match.end()))
    chars=[];positions=[];last_folded=False
    for index,char in enumerate(text):
        if not keep[index]:continue
        folded=char.isspace() and not protected[index]
        if folded and last_folded:
            positions[-1]=(positions[-1][0],index+1)
        else:
            chars.append(' ' if folded else char)
            positions.append((index,index+1))
        last_folded=folded
    return ''.join(chars),positions,wrappers


def match_quote(quote, excerpt):
    """Return original quote/span/method, or None when literal matching fails.

    Span addresses the supplied excerpt, not its enclosing document. Exact
    matches take precedence. This establishes quotation presence, not support
    for a claim's meaning.
    """
    if not isinstance(quote,str) or not isinstance(excerpt,str) or not quote.strip():
        return None
    start=excerpt.find(quote)
    if start>=0:
        return {'quote':quote,'provided_quote':quote,'span':_span(start,start+len(quote)), 'match_method':'exact'}
    projected,positions,wrappers=_project(excerpt)
    sought=_project(quote)[0].strip()
    if not sought:return None
    found=projected.find(sought)
    if found<0:return None
    start=positions[found][0];end=positions[found+len(sought)-1][1]
    # Include paired syntax surrounding a wholly selected label/code/emphasis.
    # This keeps common complete-label matches readable as original Markdown.
    changed=True
    while changed:
        changed=False
        for opening,left,right,closing in wrappers:
            if start<=left and end>=right and (opening<start or closing>end):
                start=min(start,opening);end=max(end,closing);changed=True
    return {'quote':excerpt[start:end],'provided_quote':quote,'span':_span(start,end),
            'match_method':'markdown_display','projection_version':PROJECTION_VERSION}

"""Certificate evidence from the already-owned HTTP/browser connection.

Schema and dict/export interface adapted from Crawl4AI ssl_certificate.py,
commit 838869bdda93f25e6d50b3f50ae9577d9b19a586 (Apache-2.0).
The original extra socket connection is replaced by shared HTTPX/CDP transport;
cryptography replaces OpenSSL and fixes the upstream ASCII-hex fingerprint.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from pathlib import Path
import uuid
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization


class SSLCertificate(dict):
    @classmethod
    def from_der(cls, der):
        if not isinstance(der, bytes) or not 1 <= len(der) <= 1_000_000:
            raise ValueError('Invalid certificate size')
        cert = x509.load_der_x509_certificate(der)
        def name(value):
            # Preserve repeated RDNs as well as the upstream convenience mapping.
            return [{'oid': part.oid.dotted_string, 'name': part.oid._name, 'value': part.value}
                    for part in value]
        subject, issuer = name(cert.subject), name(cert.issuer)
        return cls(subject={p['name']:p['value'] for p in subject}, issuer={p['name']:p['value'] for p in issuer},
            subject_attributes=subject, issuer_attributes=issuer, version=cert.version.value,
            serial_number=hex(cert.serial_number), not_before=cert.not_valid_before_utc.isoformat(),
            not_after=cert.not_valid_after_utc.isoformat(), fingerprint=cert.fingerprint(hashes.SHA256()).hex(),
            fingerprint_algorithm='sha256 DER bytes', signature_algorithm=cert.signature_algorithm_oid.dotted_string,
            raw_cert=base64.b64encode(der).decode('ascii'), extensions=[{'oid':ext.oid.dotted_string,
                'name':ext.oid._name,'critical':ext.critical,'value':str(ext.value)} for ext in cert.extensions])

    @property
    def subject(self): return self.get('subject', {})
    @property
    def issuer(self): return self.get('issuer', {})
    @property
    def valid_from(self): return self.get('not_before', '')
    @property
    def valid_until(self): return self.get('not_after', '')
    @property
    def fingerprint(self): return self.get('fingerprint', '')
    def to_json(self, filepath=None):
        value=json.dumps(self, ensure_ascii=False, indent=2)
        if filepath is not None: Path(filepath).write_text(value,encoding='utf-8'); return None
        return value
    def to_der(self, filepath=None):
        value=base64.b64decode(self['raw_cert'],validate=True)
        if filepath is not None: Path(filepath).write_bytes(value); return None
        return value
    def to_pem(self, filepath=None):
        value=x509.load_der_x509_certificate(self.to_der()).public_bytes(serialization.Encoding.PEM).decode('ascii')
        if filepath is not None: Path(filepath).write_text(value,encoding='ascii'); return None
        return value


def http_certificate(response):
    """Observe the current connection before response close; never open a socket."""
    if response.url.scheme != 'https': return {'status':'not_tls'}
    stream=response.extensions.get('network_stream')
    try:
        connection=stream.get_extra_info('ssl_object') if stream is not None else None
        der=connection.getpeercert(binary_form=True) if connection else None
        if not der: return {'status':'unavailable','reason':'transport_did_not_expose_certificate'}
        return {'status':'observed','scope':'shared HTTP connection leaf certificate',
            'certificate':SSLCertificate.from_der(der),'protocol':connection.version(),
            'cipher':connection.cipher(),'chain_complete':False,
            'verification':'shared HTTP client TLS verification; no independent chain audit'}
    except (ValueError, TypeError, AttributeError):
        return {'status':'unavailable','reason':'certificate_parse_failed'}


async def browser_certificates(page, response, *, ignore_https_errors=False, session=None):
    if response is None: return {'status':'unavailable','reason':'no_navigation_response'}
    if urlsplit(response.url).scheme != 'https': return {'status':'not_tls'}
    cdp=session
    try:
        async with asyncio.timeout(5):
            if cdp is None:cdp=await page.context.new_cdp_session(page)
            parts=urlsplit(response.url)
            origin=f'{parts.scheme}://{parts.netloc}'
            result=await cdp.send('Network.getCertificate',{'origin':origin})
            encoded=result.get('tableNames',[])
            if not encoded:return {'status':'unavailable','reason':'browser_did_not_expose_certificates'}
            if len(encoded)>16 or sum(len(item) for item in encoded)>2_000_000:
                return {'status':'unavailable','reason':'certificate_chain_budget'}
            certificates=[SSLCertificate.from_der(base64.b64decode(item,validate=True)) for item in encoded]
            return {'status':'observed','scope':'certificates returned by owned browser connection',
                'certificate':certificates[0], 'chain':certificates, 'chain_complete':False,
                'verification':'browser errors explicitly ignored' if ignore_https_errors else 'browser default verification; no independent chain audit'}
    except (Exception,):
        return {'status':'unavailable','reason':'browser_certificate_unavailable'}
    finally:
        if cdp is not None and session is None:
            try: await cdp.detach()
            except Exception: pass


async def save_certificate_evidence(context, url, evidence):
    """Only public certificate bytes; no session or request headers enter files."""
    if evidence.get('status')!='observed': return evidence
    leaf=SSLCertificate(evidence['certificate'])
    directory=context.paths.run(context.job_id)
    stem=hashlib.sha256(url.encode()).hexdigest()[:24]+'.tls'
    outputs, failed = {}, []
    for suffix, data in [('json',json.dumps(evidence,ensure_ascii=False,indent=2).encode()),
                         ('pem',leaf.to_pem().encode('ascii')),('der',leaf.to_der())]:
        path=directory/(stem+'.'+suffix)
        temporary=path.with_suffix(path.suffix+'.'+uuid.uuid4().hex+'.tmp')
        # Keep write+replace+cleanup in one worker operation: cancelling the
        # await must not leave a background writer recreating a removed temp.
        def publish(target=path, staging=temporary, content=data):
            try:
                staging.write_bytes(content)
                staging.replace(target)
            finally:
                try: staging.unlink(missing_ok=True)
                except OSError: pass  # Never remove anything except this call's staging path.
        try:
            writing = asyncio.create_task(asyncio.to_thread(publish))
            try:
                await asyncio.shield(writing)
            except asyncio.CancelledError:
                # File threads cannot be cancelled. Settle this owned write
                # before job resources close, then propagate cancellation.
                while not writing.done():
                    try: await asyncio.shield(writing)
                    except asyncio.CancelledError: pass
                    except OSError: break
                if writing.done() and not writing.cancelled():
                    writing.exception()
                raise
        except OSError:
            failed.append(suffix)
        else:
            outputs[suffix]={'path':str(path),'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data)}
    result = {**evidence,'exports':outputs}
    if failed:
        result.update(status='unavailable', reason='certificate_export_failed', failed_exports=failed)
    return result

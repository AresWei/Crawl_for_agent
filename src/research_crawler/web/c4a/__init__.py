# Source-derived from Crawl4AI 838869bdda93f25e6d50b3f50ae9577d9b19a586, crawl4ai/script/__init__.py.
# Copyright original Crawl4AI authors; see LICENSE.crawl4ai and ../NOTICE.
# Modifications: Attribution header; normalized UTF-8 LF encoding.
"""
C4A-Script: A domain-specific language for web automation in Crawl4AI
"""

from .c4a_compile import C4ACompiler, compile, validate, compile_file
from .c4a_result import (
    CompilationResult, 
    ValidationResult, 
    ErrorDetail, 
    WarningDetail,
    ErrorType, 
    Severity, 
    Suggestion
)

__all__ = [
    # Main compiler
    "C4ACompiler",
    
    # Convenience functions
    "compile",
    "validate", 
    "compile_file",
    
    # Result types
    "CompilationResult",
    "ValidationResult",
    "ErrorDetail",
    "WarningDetail",
    
    # Enums
    "ErrorType",
    "Severity",
    "Suggestion"
]
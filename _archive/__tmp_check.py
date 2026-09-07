import sys
sys.path.insert(0, '.')
import cleaner
print(f'APP_NAME:    {cleaner.APP_NAME}')
print(f'APP_VERSION: {cleaner.APP_VERSION}')
print(f'CLEAN_TARGETS count: {len(cleaner.CLEAN_TARGETS)}')
sample_ids = [t.get('id') for t in cleaner.CLEAN_TARGETS[:8]]
print(f'Sample target ids: {sample_ids}')
print(f'Has Analyzer: {hasattr(cleaner, "Analyzer")}')
print(f'Has Scanner: {hasattr(cleaner, "Scanner")}')
print(f'Has Cleaner: {hasattr(cleaner, "Cleaner")}')
print(f'Has _logger: {hasattr(cleaner, "_logger")}')
print(f'Has _LOG_FILE: {hasattr(cleaner, "_LOG_FILE")}')
# Test Scanner API
import inspect
sig = inspect.signature(cleaner.Scanner)
print(f'Scanner.__init__ args: {list(sig.parameters)}')
sig = inspect.signature(cleaner.Cleaner)
print(f'Cleaner.__init__ args: {list(sig.parameters)}')

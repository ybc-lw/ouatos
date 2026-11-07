"""Font/scale constants shared by build/gen_font.py and the runtime toolkit."""
# logical (scale 1.0) sizes of every named font the UI uses: name -> (face, pixels)
NAMED = {'small': ('sr', 13), 'body': ('sr', 15), 'bodyb': ('sb', 15), 'lead': ('sr', 18),
         'h2': ('sb', 22), 'h1': ('sb', 32), 'mono': ('mr', 13)}
SCALES = [1.0, 1.25, 1.5, 2.0, 2.5, 3.0]
CHARS = [chr(c) for c in range(32, 127)] + list('•·✓→←↑↓…–—×▸▾●○⚠')

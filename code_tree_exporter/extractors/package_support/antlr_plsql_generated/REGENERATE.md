# Regeneration (maintainers only)

1. Check out grammars-v4 commit `e756f2a2ee5565a9300666f100ba6acd874664f7`.
2. In `sql/plsql`, apply the repository's Python transform: replace grammar action references `this.` with `self.`.
3. Download ANTLR 4.13.2 outside this repository.
4. Generate:

   `java -jar /path/to/antlr-4.13.2-complete.jar -Dlanguage=Python3 -visitor PlSqlLexer.g4 PlSqlParser.g4`

5. Copy `PlSqlLexer.py`, `PlSqlParser.py`, `PlSqlParserVisitor.py`, `PlSqlLexerBase.py`, and `PlSqlParserBase.py` here.
6. Change generated/base-class sibling imports to package-relative imports.
7. Run `python -m unittest discover -s tests`.

Never commit the ANTLR jar. Update `PROVENANCE.md` with the exact grammars-v4 revision when pinning one.

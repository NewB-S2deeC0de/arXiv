"""Explicit source map built from A1 text/origin pairs, never inferred from A2 spans.

A1 reads text with universal newline conversion. Origins refer to that decoded
text, not original bytes; a source-map caller must use the same convention.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class MappedSourceSpan:
    path: str
    start: int
    end: int


@dataclass(frozen=True)
class SourceMapPiece:
    start: int
    end: int
    origin: MappedSourceSpan


@dataclass(frozen=True)
class RawSourceMap:
    raw: str
    pieces: tuple[SourceMapPiece, ...]

    def __post_init__(self):
        if not isinstance(self.raw, str) or not isinstance(self.pieces, tuple):
            raise TypeError('raw must be str and pieces must be tuple')
        cursor = 0
        for piece in self.pieces:
            origin = piece.origin
            if (type(piece.start) is not int or type(piece.end) is not int
                    or type(origin.start) is not int or type(origin.end) is not int
                    or not isinstance(origin.path, str) or not origin.path
                    or piece.start != cursor or piece.end <= piece.start
                    or origin.start < 0 or origin.end - origin.start != piece.end - piece.start):
                raise ValueError('Map pieces must exactly partition raw with equal-length source spans')
            cursor = piece.end
        if cursor != len(self.raw):
            raise ValueError('Map pieces must cover all raw characters')

    def _validate_range(self, start, end):
        if type(start) is not int or type(end) is not int or not 0 <= start <= end <= len(self.raw):
            raise ValueError('Expected Unicode character offsets 0 <= start <= end <= len(raw)')

    def resolve(self, start, end):
        """Keep ordered/repeated occurrences; boundary points use following piece.

        EOF point uses the last piece. Empty input has no attributable origin.
        """
        self._validate_range(start, end)
        if start == end:
            for piece in self.pieces:
                if piece.start <= start < piece.end:
                    at = piece.origin.start + start - piece.start
                    return (MappedSourceSpan(piece.origin.path, at, at),)
            if self.pieces and start == len(self.raw):
                origin = self.pieces[-1].origin
                return (MappedSourceSpan(origin.path, origin.end, origin.end),)
            return ()
        out = []
        for piece in self.pieces:
            left, right = max(start, piece.start), min(end, piece.end)
            if left < right:
                offset = piece.origin.start - piece.start
                out.append(MappedSourceSpan(piece.origin.path, left + offset, right + offset))
        return tuple(out)

    def slice(self, start, end):
        """Create a node/block map only from a known exact slice of this raw."""
        self._validate_range(start, end)
        pieces = []
        for piece in self.pieces:
            left, right = max(start, piece.start), min(end, piece.end)
            if left < right:
                offset = piece.origin.start - piece.start
                pieces.append(SourceMapPiece(left - start, right - start,
                    MappedSourceSpan(piece.origin.path, left + offset, right + offset)))
        return RawSourceMap(self.raw[start:end], tuple(pieces))


def source_map_from_gathered(gathered):
    """Consume actual A1 GatheredVersion segments by their text/origin fields."""
    pieces, texts = [], []
    cursor = 0
    for segment in gathered.segments:
        text, origin = segment.text, segment.origin
        if not isinstance(text, str):
            raise TypeError('GatheredSegment.text must be str')
        if (type(origin.start) is not int or type(origin.end) is not int or origin.start < 0
                or not isinstance(origin.path, str) or not origin.path
                or origin.end - origin.start != len(text)):
            raise ValueError('Gathered text length does not match origin; cannot claim exact mapping')
        if text:
            pieces.append(SourceMapPiece(cursor, cursor + len(text),
                MappedSourceSpan(origin.path, origin.start, origin.end)))
        texts.append(text)
        cursor += len(text)
    return RawSourceMap(''.join(texts), tuple(pieces))


def source_map_from_spans(raw, spans, source_texts):
    """Verify A2 raw against ordered spans and caller-supplied A1-decoded texts.

    No filesystem access. Missing source/edited raw fails closed instead of
    assuming that length alone implies a correct source map.
    """
    pieces, cursor = [], 0
    if not isinstance(raw, str):
        raise TypeError('raw must be str')
    for span in spans:
        if (not isinstance(span.path, str) or not span.path
                or type(span.start) is not int or type(span.end) is not int
                or not 0 <= span.start <= span.end):
            raise ValueError('Invalid source span')
        text = source_texts[span.path]
        if not isinstance(text, str):
            raise TypeError('Source texts must be decoded strings matching A1 newline semantics')
        if span.end > len(text):
            raise ValueError('Span exceeds supplied source text')
        fragment = text[span.start:span.end]
        if raw[cursor:cursor + len(fragment)] != fragment:
            raise ValueError('A2 raw does not match supplied source spans/text; exact map unavailable')
        if fragment:
            pieces.append(SourceMapPiece(cursor, cursor + len(fragment),
                MappedSourceSpan(span.path, span.start, span.end)))
        cursor += len(fragment)
    return RawSourceMap(raw, tuple(pieces))

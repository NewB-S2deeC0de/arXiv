"""Explicit map tests plus integration with the user's actual A1 snapshot."""
from copy import deepcopy
import importlib.util
import os
import subprocess
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as Obj
from types import ModuleType
import unittest

from standardization import (source_map_from_gathered, RawSourceMap, SourceMapPiece,
    MappedSourceSpan, extract_gathered_bibliography, extract_parser_bibliography,
    normalize_parser_leaf, source_map_from_spans)


def segments(*parts):
    return Obj(segments=[Obj(text=text, origin=Obj(path=path, start=start, end=start + len(text)))
                         for text, path, start in parts])


class SourceMappingTests(unittest.TestCase):
    def test_cross_file_spans_and_repeated_occurrences_are_preserved(self):
        mapping = source_map_from_gathered(segments(('Đầu', 'main.tex', 10),
            ('body', 'part.tex', 0), ('body', 'part.tex', 0)))
        self.assertEqual(mapping.raw, 'Đầubodybody')
        self.assertEqual(mapping.resolve(1, 9), (
            MappedSourceSpan('main.tex', 11, 13), MappedSourceSpan('part.tex', 0, 4),
            MappedSourceSpan('part.tex', 0, 2)))

    def test_slice_rebases_local_offsets_without_changing_source_offsets(self):
        mapping = source_map_from_gathered(segments(('abc', 'a.tex', 20), ('def', 'b.tex', 10)))
        sliced = mapping.slice(2, 5)
        self.assertEqual(sliced.raw, 'cde')
        self.assertEqual(sliced.resolve(0, 3), (MappedSourceSpan('a.tex', 22, 23),
                                               MappedSourceSpan('b.tex', 10, 12)))
        self.assertEqual(mapping.resolve(3, 3), (MappedSourceSpan('b.tex', 10, 10),))
        self.assertEqual(mapping.resolve(6, 6), (MappedSourceSpan('b.tex', 13, 13),))
        self.assertEqual(mapping.slice(1, 1).resolve(0, 0), ())

    def test_invalid_maps_and_ranges_are_rejected(self):
        with self.assertRaises(ValueError):
            RawSourceMap('abc', (SourceMapPiece(0, 3, MappedSourceSpan('a', 0, 2)),))
        with self.assertRaises(ValueError):
            source_map_from_gathered(Obj(segments=[Obj(text='abc', origin=Obj(path='a', start=0, end=5))]))
        mapping = RawSourceMap('', ())
        for start, end in ((0, 1), (-1, 0), (True, 0)):
            with self.assertRaises(ValueError):
                mapping.resolve(start, end)

    def test_span_mapping_requires_actual_matching_text_not_only_equal_lengths(self):
        spans = [Obj(path='a', start=1, end=3)]
        self.assertEqual(source_map_from_spans('bc', spans, {'a': 'abcd'}).raw, 'bc')
        with self.assertRaises(ValueError):
            source_map_from_spans('XX', spans, {'a': 'abcd'})
        with self.assertRaises(KeyError):
            source_map_from_spans('bc', spans, {})

    def test_exact_warning_mapping_and_stale_raw_rejection(self):
        raw = 'Nguyễn $bad'
        mapping = source_map_from_gathered(segments((raw, 'p.tex', 30)))
        node = Obj(occurrence_id=1, kind='sentence', raw=raw, source_spans=[], children=[], warnings=[])
        result = normalize_parser_leaf(node, after_a3=True, mode='prose', source_map=mapping)
        warning = result.diagnostics[0]
        self.assertEqual(warning.source_mapping, 'exact_segments')
        self.assertEqual(warning.source_spans, (MappedSourceSpan('p.tex', 37, 41),))
        node.raw = 'Changed ' + raw
        with self.assertRaises(ValueError):
            normalize_parser_leaf(node, after_a3=True, mode='prose', source_map=mapping)


DEFAULT_A1 = Path(__file__).resolve().parents[2] / 'New src/arXiv/src/parser/gather.py'






if __name__ == '__main__':
    unittest.main()

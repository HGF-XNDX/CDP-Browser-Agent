"""Explicit, bounded label transformations on source observations."""
from __future__ import annotations

import time

import regex


def label_values(tree, first, heading, spec, max_values, deadline):
    origin = spec.get('key_source')
    node = first
    raw = heading
    if origin:
        selected = tree.select(origin.get('selector', '.'), first)
        if len(selected) != 1:
            raise ValueError('key_source must select exactly one relative node')
        node = selected[0]
        raw = node.attrs.get(origin['attribute']) if origin.get('attribute') else node.text().strip()
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError('key_source selected a missing or non-string label')
    values = [raw.strip()]
    trace, diagnostics = [], []
    steps = spec.get('key_transforms', [])
    if steps and (spec.get('key_pattern') or spec.get('key_separator')):
        raise ValueError('Choose key_transforms OR legacy key_pattern/key_separator, not both')
    for index, step in enumerate(steps):
        before, after = values, []
        pattern = regex.compile(step['pattern']) if step['operation'] in {'capture', 'split'} else None
        for value in before:
            if time.monotonic() > deadline:
                raise ValueError('Document label transformation time budget exceeded')
            if step['operation'] == 'capture':
                match = pattern.search(value, timeout=.05)
                if not match:
                    raise ValueError(f'Label capture step {index} did not match source value {value[:160]!r}')
                group = 1 if match.lastindex else 0
                captured = match.group(group)
                if not captured or not captured.strip():
                    raise ValueError('Label capture produced an empty value')
                after.append(captured.strip())
                following = steps[index + 1] if index + 1 < len(steps) else None
                if following and following['operation'] == 'split':
                    suffix = value[match.end(group):]
                    delimiter = regex.compile(following['pattern']).match(suffix, timeout=.05)
                    if delimiter and delimiter.end() > 0:
                        diagnostics.append({'extracted_key': captured[:240], 'unconsumed_suffix': suffix[:240],
                            'step_index': index, 'message': 'The next split delimiter occurs outside the captured label. Splitting cannot recover that omitted suffix.'})
            elif step['operation'] == 'split':
                start = 0
                for match in pattern.finditer(value, timeout=.05):
                    if match.start() == match.end():
                        raise ValueError('Label split must consume a nonempty delimiter')
                    after.append(value[start:match.start()].strip())
                    start = match.end()
                    if len(after) > max_values:
                        raise ValueError('Label expansion limit exceeded')
                after.append(value[start:].strip())
                if any(not item for item in after):
                    raise ValueError('Label split produced an empty value')
            else:
                delimiter = step['delimiter']
                if delimiter not in value:
                    after.append(value)
                else:
                    ends = value.split(delimiter)
                    if len(ends) != 2 or not all(regex.fullmatch(r'[0-9]{1,10}', end.strip()) for end in ends):
                        raise ValueError('integer_range requires two nonnegative integers; other labels are not guessed')
                    low, high = map(lambda end: int(end.strip()), ends)
                    count = high - low + 1
                    if count < 1 or count > min(max_values, step.get('max_values', 1000)) or len(after) + count > max_values:
                        raise ValueError('Integer range is reversed or exceeds label expansion limit')
                    after.extend(str(number) for number in range(low, high + 1))
            if len(after) > max_values:
                raise ValueError('Label expansion limit exceeded')
        trace.append({'operation': step['operation'], 'input': before, 'output': after})
        values = after
    if len(set(values)) != len(values):
        raise ValueError('Label transformations produced duplicate labels for one source unit')
    return values, {'source_path': node.path, 'attribute': origin.get('attribute') if origin else None,
        'input': raw, 'steps': trace, 'output_labels': values}, diagnostics

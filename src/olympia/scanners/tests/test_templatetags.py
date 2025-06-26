from olympia.scanners.templatetags.scanners import format_scanners_data


def test_format_scanners_data_simple_string():
    assert format_scanners_data('fôobar') == 'fôobar'
    assert (
        format_scanners_data('<script>alert("fôobar")</script>')
        == '&lt;script&gt;alert(&quot;fôobar&quot;)&lt;/script&gt;'
    )


def test_format_scanners_data_simple_numeric():
    assert format_scanners_data(54.32) == '54.32'
    assert format_scanners_data(677890) == '677890'


def test_format_scanners_data_simple_boolean():
    assert format_scanners_data(True) == 'True'
    assert format_scanners_data(False) == 'False'


def test_format_scanners_data_simple_none():
    assert format_scanners_data(None) == 'None'


def test_format_scanners_data_simple_list():
    expected = """<ul>
<li>&lt;b&gt;fôo&lt;/b&gt;</li>
<li>bar</li>
</ul>"""
    assert format_scanners_data(['<b>fôo</b>', 'bar']) == expected
    assert format_scanners_data(('<b>fôo</b>', 'bar')) == expected
    assert format_scanners_data({'<b>fôo</b>', 'bar'}) == expected


def test_format_scanners_data_simple_dict():
    expected = """<dl>
<dt>&lt;b&gt;fôo&lt;/b&gt;:</dt><dd>bar</dd>
<dt>more:</dt><dd>xxx</dd>
</dl>"""
    assert format_scanners_data({'<b>fôo</b>': 'bar', 'more': 'xxx'}) == expected


def test_format_scanners_data_complex():
    data = [
        {'THIS IS': ['Sparta', '!']},
        {'WITNESSME': {'ratio': 0.45676895, 'blah': 'something'}},
        {'extensionId': '@welp'},
    ]
    expected_url = 'http://testserver/en-US/reviewers/review/@welp'
    expected = """<ul>
<li><dl>
<dt>THIS IS:</dt><dd><ul>
<li>Sparta</li>
<li>!</li>
</ul></dd>
</dl></li>
<li><dl>
<dt>WITNESSME:</dt><dd><dl>
<dt>ratio:</dt><dd>45.68%</dd>
<dt>blah:</dt><dd>something</dd>
</dl></dd>
</dl></li>
<li><dl>
<dt>extensionId:</dt><dd><a href="{expected_url}">@welp</a></dd>
</dl></li>
</ul>""".format(expected_url=expected_url)
    assert format_scanners_data(data) == expected

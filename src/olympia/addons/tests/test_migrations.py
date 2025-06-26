from ..migrations import fix_contributions_url


def test_fix_contributions_url():
    assert (
        fix_contributions_url('http://github.com/foo12/?q=z')
        == 'https://github.com/foo12/?q=z'
    )

    assert (
        fix_contributions_url('https://paypal.com/foo?q=z')
        == 'https://www.paypal.com/foo?q=z'
    )
    assert (
        fix_contributions_url('https://www.donate.mozilla.org/')
        == 'https://donate.mozilla.org/'
    )

    assert fix_contributions_url('https://bunny.github.com') == ''

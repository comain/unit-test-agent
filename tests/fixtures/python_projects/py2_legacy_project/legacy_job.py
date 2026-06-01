# Python 2 syntax fixture. Do not import this file from Python 3 tests.


def legacy_total(rows):
    total = 0
    for row in rows:
        try:
            total += int(row.get("qty", 0))
        except ValueError, exc:
            raise RuntimeError("bad qty: %s" % exc)
    return total


def legacy_ratio(left, right):
    if right == 0:
        return 0
    return left / right


if __name__ == "__main__":
    print 'legacy fixture ready'

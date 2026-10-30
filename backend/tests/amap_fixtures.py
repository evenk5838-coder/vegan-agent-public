"""Synthetic administrative responses shared by restaurant regression doubles."""
def district_response(keyword):
    regions = {
        '北京': ('北京市', '110000', 'province', [('朝阳区', '110105'), ('海淀区', '110108'), ('东城区', '110101')]),
        '上海': ('上海市', '310000', 'province', [('静安区', '310106'), ('浦东新区', '310115')]),
    }
    name, code, level, children = regions.get(keyword, (keyword, '990000', 'city', []))
    return {'status': '1', 'districts': [{'name': name, 'adcode': code, 'level': level,
            'districts': [{'name': n, 'adcode': c, 'level': 'district'} for n, c in children]}]}

package com.tigercode.evcollector.core.model

object CityDistricts {
    const val SEARCH_KEYWORD = "重卡充电站"

    val cityDistricts: LinkedHashMap<String, List<String>> = linkedMapOf(
        "郑州" to listOf("中原区", "二七区", "管城回族区", "金水区", "上街区", "惠济区", "中牟县", "巩义市", "荥阳市", "新密市", "新郑市", "登封市"),
        "洛阳" to listOf("老城区", "西工区", "瀍河区", "涧西区", "偃师区", "孟津区", "洛龙区", "新安县", "栾川县", "嵩县", "汝阳县", "宜阳县", "洛宁县", "伊川县"),
        "开封" to listOf("龙亭区", "顺河回族区", "鼓楼区", "禹王台区", "祥符区", "杞县", "通许县", "尉氏县", "兰考县"),
        "南阳" to listOf("宛城区", "卧龙区", "南召县", "方城县", "西峡县", "镇平县", "内乡县", "淅川县", "社旗县", "唐河县", "新野县", "桐柏县", "邓州市"),
        "许昌" to listOf("魏都区", "建安区", "鄢陵县", "襄城县", "禹州市", "长葛市"),
        "平顶山" to listOf("新华区", "卫东区", "石龙区", "湛河区", "宝丰县", "叶县", "鲁山县", "郏县", "舞钢市", "汝州市"),
        "新乡" to listOf("红旗区", "卫滨区", "凤泉区", "牧野区", "新乡县", "获嘉县", "原阳县", "延津县", "封丘县", "卫辉市", "辉县市", "长垣市"),
        "安阳" to listOf("文峰区", "北关区", "殷都区", "龙安区", "安阳县", "汤阴县", "滑县", "内黄县", "林州市"),
        "焦作" to listOf("解放区", "中站区", "马村区", "山阳区", "修武县", "博爱县", "武陟县", "温县", "沁阳市", "孟州市"),
        "商丘" to listOf("梁园区", "睢阳区", "民权县", "睢县", "宁陵县", "柘城县", "虞城县", "夏邑县", "永城市"),
        "周口" to listOf("川汇区", "淮阳区", "扶沟县", "西华县", "商水县", "沈丘县", "郸城县", "太康县", "鹿邑县", "项城市"),
        "驻马店" to listOf("驿城区", "西平县", "上蔡县", "平舆县", "正阳县", "确山县", "泌阳县", "汝南县", "遂平县", "新蔡县"),
        "信阳" to listOf("浉河区", "平桥区", "罗山县", "光山县", "新县", "商城县", "固始县", "潢川县", "淮滨县", "息县"),
        "漯河" to listOf("源汇区", "郾城区", "召陵区", "舞阳县", "临颍县"),
        "三门峡" to listOf("湖滨区", "陕州区", "渑池县", "卢氏县", "义马市", "灵宝市"),
        "鹤壁" to listOf("鹤山区", "山城区", "淇滨区", "浚县", "淇县"),
        "濮阳" to listOf("华龙区", "清丰县", "南乐县", "范县", "台前县", "濮阳县"),
        "济源" to listOf("济源"),
    )

    fun searchQueries(): List<Pair<String, String>> {
        val queries = mutableListOf<Pair<String, String>>()
        for ((city, districts) in cityDistricts) {
            for (district in districts) {
                val query = if (district == city) "$city$SEARCH_KEYWORD" else "$city$district$SEARCH_KEYWORD"
                queries.add(city to query)
            }
            queries.add(city to "$city$SEARCH_KEYWORD")
        }
        return queries
    }
}

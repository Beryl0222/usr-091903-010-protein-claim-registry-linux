"""TM184C 场景的完整构造器：供测试与演示复用。

按时间顺序建出：
1. 蛋白（含别名、酵母同源）、结构预测版本与计算批次；
2. 人源细胞连接 / 自噬实验：样本、对照、观察、统计分析；
3. 酵母救援实验；
4. 粒度明确的五类主张与证据连接（含主张间依赖链）；
5. 一篇引用主张的论文草稿。
"""

from registry.vocab import (
    CLAIM_DISEASE_TARGET,
    CLAIM_FUNCTIONAL_INVOLVEMENT,
    CLAIM_ORTHOLOG_RESCUE,
    CLAIM_RECEPTOR_IDENTITY,
    CLAIM_STRUCTURAL_SIMILARITY,
    RELATION_EXTRAPOLATION,
    RELATION_SUPPORTS,
)


def build_tm184c_scenario(r):
    r.register_protein(
        "P_TM184C", "TM184C", "human",
        tax_id="9606", aliases=["TMEM184C", "transmembrane-184C"],
        target_public=True,
    )
    r.register_protein("P_YEAST", "TM184C 酵母同源蛋白", "S. cerevisiae", tax_id="559292")
    r.link_ortholog("P_TM184C", "P_YEAST", note="序列同源，用于救援实验")

    r.register_computation_batch(
        "B_V3", "AlphaFold DB", "afdb_v3",
        search_space=">200,000,000 个预测结构",
        note="AI 从两亿余结构中识别潜在受体",
    )
    r.record_structure_prediction(
        "S_V3", "P_TM184C", "B_V3",
        target_pdb="RECEPTOR_REF", tm_score=0.84, rmsd=1.9,
    )

    # --- 人源：细胞连接 + 自噬标志物 ---------------------------------------

    r.create_experiment("E_HUMAN", "TM184C 敲除的人源细胞连接与自噬测量",
                        "human", assay="KO-phenotyping",
                        materials=["TM184C-KO HEK293 细胞系", "LC3 抗体"])
    r.register_sample("E_HUMAN", "SAM_KO", "cell_line",
                      description="TM184C 敲除细胞系", genotype="TM184C-/-")
    r.register_control("E_HUMAN", "CTL_WT", "wildtype",
                       paired_sample_id="SAM_KO", description="同背景野生型")
    r.register_sample("E_HUMAN", "SAM_RES", "cell_line",
                      description="KO 里回转 TM184C 的救援细胞系", genotype="TM184C-/- + rescue")
    r.register_control("E_HUMAN", "CTL_VEC", "empty_vector",
                       paired_sample_id="SAM_RES", description="空载体对照")

    r.record_observation(
        "O_JX", "E_HUMAN", "SAM_KO", "junction_integrity",
        -0.42, unit="z-score", controls=["CTL_WT"],
        conditions={"timepoint_h": 48},
    )
    r.record_observation(
        "O_LC3", "E_HUMAN", "SAM_KO", "LC3_puncta_per_cell",
        2.1, unit="fold over WT", controls=["CTL_WT"],
    )
    r.record_observation(
        "O_RES", "E_HUMAN", "SAM_RES", "junction_integrity",
        -0.03, unit="z-score", controls=["CTL_VEC"],
        conditions={"note": "回转恢复表型"},
    )
    r.record_statistical_analysis(
        "A_JX", "E_HUMAN", "welch_t_test",
        observation_ids=["O_JX"], sample_ids=["SAM_KO", "SAM_RES"],
        p_value=0.003, effect_size=-1.1, ci=[-1.6, -0.6],
        model_spec={"paired": False},
    )
    r.record_statistical_analysis(
        "A_LC3", "E_HUMAN", "welch_t_test",
        observation_ids=["O_LC3"], sample_ids=["SAM_KO"],
        p_value=0.011, effect_size=0.9,
    )
    r.record_statistical_analysis(
        "A_RES", "E_HUMAN", "welch_t_test",
        observation_ids=["O_RES"], sample_ids=["SAM_RES"],
        p_value=0.62, effect_size=0.05,
    )

    # --- 酵母：同源蛋白救援 -------------------------------------------------

    r.create_experiment("E_YEAST", "酵母同源蛋白救援实验", "S. cerevisiae",
                        assay="plasmid-rescue", materials=["yor1Δ 菌株", "人 TM184C 表达质粒"])
    r.register_sample("E_YEAST", "SAM_YKO", "strain",
                      description="敲除内源同源基因的酵母", genotype="yor1Δ")
    r.register_control("E_YEAST", "CTL_YVEC", "empty_vector",
                       paired_sample_id="SAM_YKO", description="转入空载体")
    r.register_sample("E_YEAST", "SAM_YHV", "strain",
                      description="yor1Δ 中表达人 TM184C", genotype="yor1Δ + pTM184C")
    r.register_control("E_YEAST", "CTL_YWT", "wildtype",
                       paired_sample_id="SAM_YHV", description="野生型酵母")
    r.record_observation(
        "O_YRES", "E_YEAST", "SAM_YHV", "fraction_surviving",
        0.95, unit="fraction", controls=["CTL_YVEC"],
        conditions={"stress": "ligand exposure"},
    )
    r.record_statistical_analysis(
        "A_YRES", "E_YEAST", "fishers_exact_test",
        observation_ids=["O_YRES"], sample_ids=["SAM_YHV"],
        p_value=0.0008, effect_size=None,
    )

    # --- 主张（粒度明确，确定程度由状态而非措辞表达）------------------------

    r.raise_claim(
        "C_STRUCT", "P_TM184C", CLAIM_STRUCTURAL_SIMILARITY,
        "human", "TM184C 预测结构与已知受体参考折叠高度相似（TM=0.84）",
        computation_batch_id="B_V3",
    )
    r.add_evidence("C_STRUCT", "structure_prediction", "S_V3",
                   RELATION_SUPPORTS, note="纯计算证据")

    r.raise_claim(
        "C_FUNC", "P_TM184C", CLAIM_FUNCTIONAL_INVOLVEMENT, "human",
        "TM184C 参与人源细胞连接完整性的调节，并影响自噬标志物水平",
    )
    r.add_evidence("C_FUNC", "analysis", "A_JX", RELATION_SUPPORTS)
    r.add_evidence("C_FUNC", "analysis", "A_LC3", RELATION_SUPPORTS)

    r.raise_claim(
        "C_RESCUE", "P_TM184C", CLAIM_ORTHOLOG_RESCUE, "human",
        "人 TM184C 可救援酵母同源基因缺失表型，提示功能跨物种保守",
    )
    r.add_evidence("C_RESCUE", "analysis", "A_YRES",
                   RELATION_EXTRAPOLATION, source_species="S. cerevisiae")

    r.raise_claim(
        "C_REC", "P_TM184C", CLAIM_RECEPTOR_IDENTITY, "human",
        "TM184C 是该信号通路的受体成员",
    )
    r.add_claim_dependency("C_REC", "C_STRUCT", note="折叠相似是动机而非证据")
    r.add_claim_dependency("C_REC", "C_FUNC")
    r.add_claim_dependency("C_REC", "C_RESCUE", RELATION_EXTRAPOLATION)
    r.add_evidence("C_REC", "analysis", "A_JX", RELATION_SUPPORTS)

    r.raise_claim(
        "C_TARGET", "P_TM184C", CLAIM_DISEASE_TARGET, "human",
        "TM184C 可作为某未公开疾病项目的干预靶点",
    )
    r.add_claim_dependency("C_TARGET", "C_REC")

    # --- 论文草稿 -----------------------------------------------------------

    r.create_paper(
        "PAPER_A", "TM184C 作为候选受体的计算识别与功能证据",
        authors=["课题组 A"], doi=None,
    )
    r.add_paper_citation("PAPER_A", "ref-1", "C_STRUCT",
                         quoted_assertion="结构预测提示 TM184C 与受体折叠相似")
    r.add_paper_citation("PAPER_A", "ref-2", "C_FUNC")
    r.add_paper_citation("PAPER_A", "ref-3", "C_RESCUE")
    r.add_paper_citation("PAPER_A", "ref-4", "C_REC")

    return {
        "proteins": ["P_TM184C", "P_YEAST"],
        "batches": ["B_V3"],
        "predictions": ["S_V3"],
        "experiments": ["E_HUMAN", "E_YEAST"],
        "claims": ["C_STRUCT", "C_FUNC", "C_RESCUE", "C_REC", "C_TARGET"],
        "papers": ["PAPER_A"],
    }

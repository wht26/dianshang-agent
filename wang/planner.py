"""Inspectable rule-based support plan, never a business authorization."""

import re


def make_plan(question, history, context, preferences):
    signals = {
        "quality": ("开胶", "破损", "开裂", "质量", "断底", "掉色"),
        "return": ("退货", "换码", "换货", "退款", "不合适"),
        "sizing": ("尺码", "几码", "选码", "脚长", "胸围", "合身"),
        "care": ("洗", "晾", "晒", "护理", "保养"),
        "delivery": ("物流", "发货", "快递", "到货", "签收"),
        "product": ("面料", "材质", "颜色", "容量", "价格"),
    }
    intents = [key for key, words in signals.items() if any(word in question for word in words)]
    relevant_history = [t for t in history[-4:] if t.get("product_id") == context["product_id"]]
    statements = " ".join(t["question"] for t in relevant_history) + " " + question
    missing = []
    if not context["product_facts"]:
        missing.append("具体商品或款式")
    if "sizing" in intents and not re.search(r"\d+\s*(?:毫米|厘米|mm|cm)", statements, re.I):
        missing.append("与该款尺码表对应的测量值及合身偏好")
    if any(intent in intents for intent in ("quality", "return")):
        if not re.search(r"(?:收到|签收|收货).{0,12}(?:天|日|昨天|今天|周)|(?:昨天|今天).{0,6}(?:收到|签收)", statements):
            missing.append("大致收货时间")
        if not any(word in statements for word in ("洗过", "没洗", "穿过", "没穿", "试穿", "未使用")):
            missing.append("使用或洗涤情况")
    live = any(word in question for word in ("查物流", "查订单", "退款进度", "帮我退款", "直接退款"))
    return {"intents": intents or ["clarify"], "product_id": context["product_id"],
            "missing_information": missing, "next_question": missing[0] if missing else None,
            "needs_external_verification": live or any(i in intents for i in ("quality", "return")),
            "steps": ["确认商品和买家目标", "检索对应商品及政策", "核对证据与缺项", "先回应已知，再问必要条件"],
            "strategy": "解释咨询范围并提供原渠道核验要点" if live else "依据资料回答，不保证责任或退换资格",
            "preferences": {k: v for k, v in preferences.items() if v},
            "planner_type": "rules-v1"}

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
联网搜索工具 - 通过 qwen enable_search 获取实时市场信息

利用通义千问的联网搜索能力，获取最新的股票行情、行业新闻、
政策动态、分析师观点等投研所需的实时数据。

用法:
    python search_market.py --query "贵州茅台最新股价" --type stock
    python search_market.py --query "半导体行业政策" --type policy
    python search_market.py --query "中芯国际分析师评级" --type general
"""

import argparse
import io
import json
import os
import sys

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from openai import OpenAI


DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY")

# 不同搜索类型对应的系统提示词
SEARCH_PROMPTS = {
    "stock": (
        "你是一位专业的股票分析助手。请根据搜索结果，提供以下信息(如果能找到):\n"
        "1. 最新股价/涨跌幅\n"
        "2. 成交量/换手率等交易数据\n"
        "3. 近期重要事件或公告\n"
        "4. 市场关注的核心逻辑\n"
        "请用简洁专业的语言回答，数据要准确，注明数据来源和时间。"
    ),
    "news": (
        "你是一位专业的财经新闻分析师。请根据搜索结果:\n"
        "1. 整理最相关的新闻要点(按时间倒序)\n"
        "2. 分析新闻对相关股票/行业的潜在影响\n"
        "3. 标注新闻来源和发布时间\n"
        "请客观中立地总结，区分事实和观点。"
    ),
    "policy": (
        "你是一位政策研究专家。请根据搜索结果:\n"
        "1. 列出最新的相关政策/法规/指导意见\n"
        "2. 分析政策要点和影响范围\n"
        "3. 评估对相关行业和公司的影响\n"
        "请引用政策原文或官方来源。"
    ),
    "general": (
        "你是一位专业的投资研究助手。请根据搜索到的最新信息，"
        "全面准确地回答用户的问题。"
        "请注明关键数据的来源和时间，确保信息的时效性和准确性。"
    ),
}


def search_with_qwen(query: str, search_type: str = "general", model: str = "qwen-plus") -> str:
    """
    使用 qwen 的联网搜索能力获取实时信息

    Args:
        query: 搜索查询
        search_type: 搜索类型 (stock/news/policy/general)
        model: 使用的模型

    Returns:
        搜索结果文本
    """
    if not DASHSCOPE_API_KEY:
        print("[错误] 请设置环境变量 DASHSCOPE_API_KEY")
        sys.exit(1)

    client = OpenAI(
        api_key=DASHSCOPE_API_KEY,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    )

    system_prompt = SEARCH_PROMPTS.get(search_type, SEARCH_PROMPTS["general"])

    print(f"[搜索] 类型={search_type}, 查询: {query}")
    print(f"[模型] {model} (enable_search=True)")

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": query},
        ],
        extra_body={"enable_search": True},
    )

    result = response.choices[0].message.content
    return result


def main():
    parser = argparse.ArgumentParser(description="联网搜索工具")
    parser.add_argument("--query", required=True, help="搜索查询")
    parser.add_argument(
        "--type",
        choices=["stock", "news", "policy", "general"],
        default="general",
        help="搜索类型: stock=个股行情, news=财经新闻, policy=政策法规, general=通用搜索",
    )
    parser.add_argument("--model", default="qwen-plus", help="模型(默认 qwen-plus)")
    parser.add_argument("--output", default=None, help="结果保存路径(可选)")
    args = parser.parse_args()

    result = search_with_qwen(args.query, args.type, args.model)

    print(f"\n{'=' * 60}")
    print(f"[搜索结果]\n{result}")
    print(f"{'=' * 60}")

    if args.output:
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        output_data = {
            "query": args.query,
            "type": args.type,
            "result": result,
        }
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(output_data, f, ensure_ascii=False, indent=2)
        print(f"[保存] 结果已保存: {args.output}")

    result_json = {
        "status": "success",
        "query": args.query,
        "type": args.type,
        "answer": result,
    }
    print(f"\n[结果JSON] {json.dumps(result_json, ensure_ascii=False, indent=2)}")


if __name__ == "__main__":
    main()

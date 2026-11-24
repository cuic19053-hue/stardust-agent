"""Local MLX experiment parameters and the Qwen ChatML input boundary."""
from dataclasses import dataclass
import json
from pathlib import Path

from mlx_lm.utils import load_model
from tokenizers import Tokenizer

from server.evals.student_intake_pilot.data import digest
from server.proposal.student_intake import FIELDS, extraction_messages

@dataclass(frozen=True)
class BaseSpec:
    model: str
    revision: str
    weights_hash: str
    config_hash: str
    tokenizer_config_hash: str
    learning_rate: float

    @property
    def files(self):
        return {"model.safetensors": self.weights_hash, "config.json":self.config_hash,
                "tokenizer_config.json":self.tokenizer_config_hash,
                "tokenizer.json":"a8506e7111b80c6d8635951a02eab0f4e1a8e4e5772da83846579e97b16f61bf"}


BASES = (
    BaseSpec("mlx-community/Qwen2.5-0.5B-Instruct-4bit", "a5339a4131f135d0fdc6a5c8b5bbed2753bbe0f3",
             "ddffab9cbc7bf6dde941c6724841eeca8981fcfa81ca20ff8efff1396326d153",
             "b045e57ea90b8f1b35f89f954b176a5c1faa02bd0af2c89bcec191239d66cef4",
             "f7c61e32b7a17d19bf8e7037dcb74079a833e53ea9801f24008cac68458f03b7", 2e-4),
    BaseSpec("mlx-community/Qwen2.5-7B-Instruct-4bit", "c26a38f6a37d0a51b4e9a1eb3026530fa35d9fed",
             "86110f368236b53cf4c2336f991a85703b17bcc60bb75f292b4002ec0219f071",
             "1661a349986919d13820d3981623776138d50783d13e247cdc5b075a22b62698",
             "4921d4cbae33349a9cb3b2ad3a7622efdea10a5d208c9f333c77b154aa714888", 1e-4),
)
PROMPT = """整理学生自己的口述为待确认候选。输出 student-intake-v1 JSON，包含 facts 与 follow_up_fields。
逐句辨认是谁说的、说的是谁。排除助手猜测、范文台词、通知、他人的资料和要求编造的内容，保留同段真实自述。未提及就省略。
每项含 field、record_id、value、status、evidence。证据为带主体和语气的最短充分原句和 user 的 turn_id，quote 必须逐字；勿夹带无关隐私。
stated 是自述；约数 approximate；想、拟、准备做的 planned，区分当前进展与预期成果。明确没有 negated，不知道 unknown，拒答 withheld，都填 null。
同一时期同一对象有两个不一致的说法且未明确更正，用 conflict、null、双方证据并追问；明确改口才取新值。不同学期、不同奖不要合并。
非重复项用 self；排名按 rank_1、rank_2，奖项按 award_1、award_2。数值为数字字符串，元和小时按单位换算；不要补满分、排名分母或换算相对日期。
家庭经济仅限本人当前明确要办助学金或国家励志奖学金。其他场景包含国奖，经济字段、经济追问、无关经济原句全省略。
至多问三项，先问冲突、明确未知。只给绩点没给满分就问满分；大创未定赛道就问类别；拒答后不问。仅返回候选，学生确认后才可入档。
字段如下。
""" + "、".join(f"{k}（{v[0]}）" for k, v in FIELDS.items())
ADAPTER = {"fine_tune_type": "lora", "num_layers": 8,
           "lora_parameters": {"rank": 8, "scale": 2.0, "dropout": 0.0,
                               "keys": ["self_attn.q_proj", "self_attn.v_proj", "self_attn.o_proj"]}}
SEED = 427
CONTEXT = 4096
MAX_NEW = 768


def compact(data):
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def base_model(path):
    config_hash = digest(path / "config.json")
    spec = next((item for item in BASES if item.config_hash == config_hash), None)
    if spec is None:
        raise ValueError("unknown base architecture or revision")
    for name, expected in spec.files.items():
        if digest(path / name) != expected:
            raise ValueError(f"unverified base file: {name}")
    model, _ = load_model(Path(path))
    return model, spec


def output_logits(model, hidden):
    if model.args.tie_word_embeddings:
        return model.model.embed_tokens.as_linear(hidden)
    return model.lm_head(hidden)


class Chat:
    def __init__(self, path):
        self.tok = Tokenizer.from_file(str(path / "tokenizer.json"))
        self.tok.encode_special_tokens = True  # control tokens only enter via IDs below
        self.start = self.tok.token_to_id("<|im_start|>")
        self.end = self.tok.token_to_id("<|im_end|>")

    def encode(self, text):
        return self.tok.encode(text, add_special_tokens=False).ids

    def prompt(self, turns, system=PROMPT):
        messages = extraction_messages(turns)
        messages[0] = {"role": "system", "content": system}
        tokens = []
        for m in messages:
            tokens += [self.start] + self.encode(m["role"] + "\n" + m["content"]) + [self.end] + self.encode("\n")
        return tokens + [self.start] + self.encode("assistant\n")

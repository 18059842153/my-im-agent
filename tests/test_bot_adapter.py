"""智能机器人适配器的纯逻辑测试（不建立真实 WebSocket 连接）。"""

import pytest

from my_im_agent.config import Settings
from my_im_agent.wecom.bot_adapter import build_inbound, strip_group_mention


class TestStripGroupMention:
    def test_single_chat_untouched(self):
        assert strip_group_mention("明天早上9点提醒我开会") == "明天早上9点提醒我开会"

    def test_group_mention_removed(self):
        assert strip_group_mention("@RobotA 明天早上9点提醒我开会") == "明天早上9点提醒我开会"

    def test_bot_name_with_space_stripped_when_configured(self):
        # 机器人叫「IM Agent」时，靠空格切分会残留 "Agent"，所以要按配置的名字精确剥离
        assert (
            strip_group_mention("@IM Agent 明天早上9点提醒我开会", bot_name="IM Agent")
            == "明天早上9点提醒我开会"
        )

    def test_leading_space_trimmed(self):
        assert strip_group_mention("   @Bot 你好") == "你好"

    def test_at_without_space_kept(self):
        # 邮箱之类不该被误伤
        assert strip_group_mention("@abc") == "@abc"


class TestBuildInbound:
    def test_text_message_single_chat(self):
        body = {
            "msgid": "m1",
            "chattype": "single",
            "from": {"userid": "ZhangSan"},
            "msgtype": "text",
            "text": {"content": "你好"},
        }

        msg = build_inbound(body)

        assert msg is not None
        assert msg.content == "你好"
        assert msg.user_id == "ZhangSan"
        assert msg.conversation_id == "ZhangSan"  # 单聊无 chatid，回落 userid
        assert msg.message_id == "m1"
        assert msg.raw["chattype"] == "single"

    def test_group_chat_uses_chatid_and_strips_mention(self):
        body = {
            "msgid": "m2",
            "chatid": "CHAT_ABC",
            "chattype": "group",
            "from": {"userid": "LiSi"},
            "msgtype": "text",
            "text": {"content": "@Robot 查看闹钟"},
        }

        msg = build_inbound(body)

        assert msg is not None
        assert msg.content == "查看闹钟"
        assert msg.conversation_id == "CHAT_ABC"
        assert msg.user_id == "LiSi"

    @pytest.mark.parametrize("msgtype", ["image", "file", "voice", "video", "mixed"])
    def test_non_text_ignored(self, msgtype):
        assert build_inbound({"msgtype": msgtype, "from": {"userid": "u"}}) is None

    def test_blank_text_ignored(self):
        body = {"msgtype": "text", "from": {"userid": "u"}, "text": {"content": "   "}}
        assert build_inbound(body) is None


class TestBotConfigGate:
    def test_missing_bot_config_fails_fast(self):
        from my_im_agent.wecom.bot_adapter import WeComBotAdapter

        with pytest.raises(RuntimeError, match="WECOM_BOT"):
            WeComBotAdapter(Settings())

    def test_bot_enabled_flag(self):
        assert not Settings().wecom_bot_enabled
        assert Settings(wecom_bot_id="b", wecom_bot_secret="s").wecom_bot_enabled

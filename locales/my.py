from config import settings

START_TEXT = f"""🇲🇲 မင်္ဂလာပါ။ {settings.bot_display_name} မှ ကြိုဆိုပါတယ်။

အသုံးပြုနည်း:
• Group ထဲမှာ photo/video ပို့ရင် auto lookup လုပ်ပေးပါမယ်။
• Manual lookup လုပ်ချင်ရင် media ကို reply ပြန်ပြီး /waifu /w .wa .w /name .name သုံးနိုင်ပါတယ်။"""

FORCE_JOIN_TEXT = """🇲🇲 Bot ကိုအသုံးပြုရန် အောက်က channel/group တွေကို join ပေးပါ။

Join ပြီးပါက ✅ Joined / Check Again ကိုနှိပ်ပါ။"""
GROUP_FORCE_JOIN_TEXT = "🇲🇲 Force join စစ်ရန် Bot DM ထဲဝင်ပြီး ဆက်လုပ်ပေးပါ။"
NOT_FOUND = "🇲🇲 Name မတွေ့ပါဘူး။ Database ထဲမှာ မရှိသေးတာ (သို့) source/media မကိုက်တာ ဖြစ်နိုင်ပါတယ်။"
NO_MEDIA = "🇲🇲 Media ကို reply ပြန်ပြီး command သုံးပေးပါ။"
NOT_APPROVED = "🇲🇲 ဒီ group မှာ auto lookup မဖွင့်ထားသေးပါ။ Owner/Sudo က /gapprove လုပ်ပေးရပါမယ်။"
JOIN_OK = "🇲🇲 Join စစ်ပြီးပါပြီ။ Bot ကိုအသုံးပြုနိုင်ပါပြီ။"
JOIN_FAIL = "🇲🇲 Join မပြီးသေးပါ။ Channel/Group တွေ join ပြီးမှ ပြန်စစ်ပါ။"
BLOCKED_SOURCE = "🇲🇲 ဒီ Bot ကို ဘမ်းထားပါတယ်"

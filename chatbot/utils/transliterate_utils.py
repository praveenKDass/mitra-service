import logging

from langfuse import observe, get_client

from chatbot.models import VoiceProvider, VoiceType, LanguageMapping, CompanyBot
from chatbot.translate.ai4Bharat.transliterate import call_ai4bharat_transliterate_api
from chatbot.translate.custom.custom_llm import handle_custom_translation
from chatbot.translate.sarvam.sarvam import SarvamLanguageService
from chatbot.utils.audio_provider_utils import get_voice_provider

logger = logging.getLogger('django')
langfuse_context = get_client()

# Placeholder rates — replace with actual provider pricing.
TRANSLITERATE_PRICING = {
    ("AI4Bharat", "transliterate"): {"unit": "character", "rate": 0.0},
    ("SARVAM", "transliterate"): {"unit": "character", "rate": 0.00002},
}


@observe(as_type="generation")
def transliterate_text(
        source_language, target_language, message_body, is_sentence=False, voice_provider=None, company_bot=None
):
    try:
        if not voice_provider and company_bot:
            voice_provider = get_voice_provider(
                company_bot=company_bot, voice_type=VoiceType.Transliterate, source_language=source_language,
                target_language=target_language
            )

        provider_name = voice_provider.provider
        char_count = len(message_body or "")

        if provider_name == VoiceProvider.AI4Bharat:
            response = call_ai4bharat_transliterate_api(
                source_language=source_language, target_language=target_language, message_body=message_body,
                is_sentence=is_sentence
            )
        elif provider_name == VoiceProvider.SARVAM:
            service = SarvamLanguageService()
            response = service.transliterate(
                input_text=message_body, source_lang=LanguageMapping.get_mapped_language(source_language),
                target_lang=LanguageMapping.get_mapped_language(target_language),
                voice_provider=voice_provider
            )
        elif provider_name == VoiceProvider.CUSTOM_LLM:
            other = getattr(voice_provider, "other_params", {}) or {}
            route = other.get('route', "/transliterate_text")
            company_bot = CompanyBot.objects.filter(route=route).first()
            # Cost is tracked inside handle_custom_translation's underlying
            # handle_openai_model / handle_bedrock_model generation — do not
            # attach cost_details here for this branch.
            response = handle_custom_translation(
                message_body=message_body, source_language=LanguageMapping.get_mapped_language(source_language),
                target_language=LanguageMapping.get_mapped_language(target_language), company_bot=company_bot
            )
        else:
            return {
                'status': 500,
                'content': "No provider found!"
            }

        try:
            if provider_name == VoiceProvider.CUSTOM_LLM:
                # No cost/usage here — real numbers live on the nested
                # handle_openai_model/handle_bedrock_model generation.
                langfuse_context.update_current_generation(
                    name=f"transliterate-{provider_name.lower()}",
                    model=f"{provider_name.lower()}-transliterate-delegate",
                    input={
                        "source_language": source_language, "target_language": target_language,
                        "char_count": char_count, "is_sentence": is_sentence
                    },
                    output={"status": response.get("status") if isinstance(response, dict) else None},
                    metadata={"delegated_to": "handle_custom_translation", "route": route}
                )
            else:
                pricing = TRANSLITERATE_PRICING.get(
                    (provider_name, "transliterate"), {"unit": "character", "rate": 0.0}
                )
                cost = char_count * pricing["rate"]
                langfuse_context.update_current_generation(
                    name=f"transliterate-{provider_name.lower()}",
                    model=f"{provider_name.lower()}-transliterate",
                    input={
                        "source_language": source_language, "target_language": target_language,
                        "char_count": char_count, "is_sentence": is_sentence
                    },
                    output={"status": response.get("status") if isinstance(response, dict) else None},
                    usage_details={"input": char_count, "unit": "characters"},
                    cost_details={"total": round(cost, 6)}
                )
        except Exception as le:
            logger.error(f"Langfuse transliterate generation logging failed: {le}")

        return response
    except Exception as e:
        try:
            langfuse_context.score_current_observation(
                name="transliteration_error",
                value=0,
                data_type="BOOLEAN",
                comment=str(e)[:200]
            )
        except Exception as se:
            logger.error(f"Langfuse score logging failed: {se}")
        return {
            'status': 500,
            'content': message_body
        }


def get_transliteration_output(data):
    if data and isinstance(data, dict):
        data = data.get('content', [])
    if data and isinstance(data, list) and len(data) > 0:
        return data[0]

    return None
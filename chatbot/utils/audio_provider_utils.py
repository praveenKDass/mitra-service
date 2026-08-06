import base64
import io
import logging
import wave

from django.conf import settings
from langfuse import observe, get_client

from chatbot.models import VoiceProvider, LanguageMapping, Voice, VoiceType, CompanyBot
from chatbot.translate.ai4Bharat.speech_to_text import transcribe_ai4bharat_multiple_chunks
from chatbot.translate.ai4Bharat.text_to_speech import ai4bharat_text_speech
from chatbot.translate.ai4Bharat.text_to_text import call_ai4bharat_translation_api
from chatbot.translate.custom.custom_llm import handle_custom_translation
from chatbot.translate.google.google_stt import transcribe_multiple_languages_v2
from chatbot.translate.google.google_stt_v1 import transcribe_multiple_languages_v1
from chatbot.translate.google.google_translate import translate_text
from chatbot.translate.google.google_tts import google_text_to_speech
from chatbot.translate.openai.openai_stt import transcribe_audio
from chatbot.translate.sarvam.sarvam import SarvamLanguageService
from chatbot.translate.sarvam.speech_to_text import transcribe_sarvam_multiple_chunks
from chatbot.translate.sarvam.text_to_speech import sarvam_text_to_speech
from chatbot.translate.shikshalokam.speech_to_text import sl_speech_to_text
from chatbot.translate.shikshalokam.text_to_speech import sl_text_to_speech
from chatbot.translate.shikshalokam.text_to_text import sl_translate

logger = logging.getLogger('django')
langfuse_context = get_client()

# --- Pricing config -----------------------------------------------------
# Placeholder rates — replace with your actual contracted/published rates
# per provider. Unit determines how usage_details is reported to Langfuse.
VOICE_PRICING = {
    ("AI4Bharat", "asr"): {"unit": "second", "rate": 0.0},
    ("SARVAM", "asr"): {"unit": "second", "rate": 0.00015},
    ("OPENAI_WHISPER", "asr"): {"unit": "minute", "rate": 0.006},
    ("GOOGLE", "asr"): {"unit": "second", "rate": 0.000067},
    ("SHIKSHALOKAM", "asr"): {"unit": "second", "rate": 0.0},

    ("AI4Bharat", "tts"): {"unit": "character", "rate": 0.0},
    ("SARVAM", "tts"): {"unit": "character", "rate": 0.00002},
    ("GOOGLE", "tts"): {"unit": "character", "rate": 0.000016},
    ("SHIKSHALOKAM", "tts"): {"unit": "character", "rate": 0.0},

    ("AI4Bharat", "translate"): {"unit": "character", "rate": 0.0},
    ("SARVAM", "translate"): {"unit": "character", "rate": 0.00002},
    ("GOOGLE", "translate"): {"unit": "character", "rate": 0.00002},
    ("CUSTOM_LLM", "translate"): {"unit": "character", "rate": 0.0},
    ("SHIKSHALOKAM", "translate"): {"unit": "character", "rate": 0.0},
}


def _get_pricing(provider_name: str, category: str) -> dict:
    return VOICE_PRICING.get((provider_name, category), {"unit": category, "rate": 0.0})


def _get_audio_duration_seconds(base64_audio: str, audio_format: str) -> float:
    """Best-effort duration extraction so cost is known even when the
    provider doesn't return duration in its response. Only handles WAV
    natively; other formats fall back to 0.0 (still logs, just no duration)."""
    try:
        raw = base64.b64decode(base64_audio)
        if audio_format == "wav":
            with wave.open(io.BytesIO(raw)) as wf:
                return wf.getnframes() / float(wf.getframerate())
    except Exception as e:
        logger.warning(f"Could not extract audio duration: {e}")
    return 0.0


def _log_generation(name, model, input_data, output_data, usage, cost, metadata=None):
    """Shared helper to keep the try/except boilerplate in one place,
    mirroring the pattern used in handle_bedrock_model."""
    try:
        langfuse_context.update_current_generation(
            name=name,
            model=model,
            input=input_data,
            output=output_data,
            usage_details=usage,
            cost_details={"total": round(cost, 6)},
            metadata=metadata or {}
        )
    except Exception as le:
        logger.error(f"Langfuse generation logging failed for {name}: {le}")


def get_voice_provider(company_bot, voice_type, source_language=None, target_language=None):
    """Return appropriate Voice provider preferring non-English language."""

    language = (
        target_language if target_language and target_language.lower() != "en"
        else source_language if source_language and source_language.lower() != "en"
        else "en"
    )

    voice = Voice.objects.filter(
        company_bot=company_bot,
        type=voice_type,
        language=language,
    ).first()

    if not voice and language != "en":
        voice = Voice.objects.filter(
            company_bot=company_bot,
            type=voice_type,
            language="en",
        ).first()

    return voice


@observe(as_type="generation")
def text_speech_provider(company_bot, text, source_language):
    voice_provider = get_voice_provider(
        company_bot=company_bot, voice_type=VoiceType.TextToSpeech, source_language=source_language
    )
    if not voice_provider:
        return {
            'status': 500,
            'content': "No voice configuration found!"
        }

    provider_name = voice_provider.provider

    if provider_name == VoiceProvider.AI4Bharat:
        response = ai4bharat_text_speech(
            text=text, gender=voice_provider.gender, source_language=source_language,
            voice_provider=voice_provider
        )
    elif provider_name == VoiceProvider.GOOGLE:
        response = google_text_to_speech(
            message=text, language_code=LanguageMapping.get_mapped_language(source_language),
            voice_provider=voice_provider
        )
    elif provider_name == VoiceProvider.SARVAM:
        response = sarvam_text_to_speech(
            message=text, source_language=source_language, voice_provider=voice_provider
        )
    elif provider_name == VoiceProvider.SHIKSHALOKAM:
        response = sl_text_to_speech(
            text=text, source_language=source_language, voice_provider=voice_provider
        )
    else:
        return {
            'status': 500,
            'content': "No provider found!"
        }

    char_count = len(text or "")
    pricing = _get_pricing(provider_name, "tts")
    cost = char_count * pricing["rate"]

    _log_generation(
        name=f"tts-{provider_name.lower()}",
        model=f"{provider_name.lower()}-tts",
        input_data={"source_language": source_language, "char_count": char_count},
        output_data={"status": response.get("status") if isinstance(response, dict) else None},
        usage={"input": char_count, "unit": "characters"},
        cost=cost,
        metadata={"gender": getattr(voice_provider, "gender", None)}
    )

    return response


@observe(as_type="generation")
def speech_text_provider(company_bot, base64, audio_format, source_language):
    voice_provider = get_voice_provider(
        company_bot=company_bot, voice_type=VoiceType.SpeechToText, source_language=source_language
    )
    if not voice_provider:
        return {
            'status': 500,
            'content': "No voice configuration found!"
        }

    provider_name = voice_provider.provider
    duration_seconds = _get_audio_duration_seconds(base64, audio_format)

    if provider_name == VoiceProvider.AI4Bharat:
        response = transcribe_ai4bharat_multiple_chunks(
            base64_audio_file=base64, source_language=source_language, audio_format=audio_format,
            voice_provider=voice_provider
        )
    elif provider_name == VoiceProvider.GOOGLE:
        if source_language == 'en':
            region = "US"
        else:
            region = "IN"

        secret = settings.SECRETS
        response = transcribe_multiple_languages_v2(
            project_id=secret.get('project_id'), audio_file=base64,
            language_codes=[LanguageMapping.get_mapped_language(source_language, region)],
            voice_provider=voice_provider
        )
    elif provider_name == VoiceProvider.OPENAI_WHISPER:
        response = transcribe_audio(
            base64_audio=base64, audio_format=audio_format, source_language=source_language,
            voice_provider=voice_provider
        )
    elif provider_name == VoiceProvider.SARVAM:
        response = transcribe_sarvam_multiple_chunks(
            base64_audio_file=base64, audio_format=audio_format,
            source_language=LanguageMapping.get_sarvam_language(source_language),
            voice_provider=voice_provider
        )
    elif provider_name == VoiceProvider.SHIKSHALOKAM:
        response = sl_speech_to_text(
            base64_audio_file=base64, source_language=source_language,
            audio_format=audio_format, voice_provider=voice_provider
        )
    else:
        return {
            'status': 500,
            'content': "No provider found!"
        }

    pricing = _get_pricing(provider_name, "asr")
    if pricing["unit"] == "minute":
        usage = {"input": round(duration_seconds / 60, 3), "unit": "minutes"}
        cost = (duration_seconds / 60) * pricing["rate"]
    else:
        usage = {"input": round(duration_seconds, 2), "unit": "seconds"}
        cost = duration_seconds * pricing["rate"]

    _log_generation(
        name=f"asr-{provider_name.lower()}",
        model=f"{provider_name.lower()}-asr",
        input_data={"source_language": source_language, "audio_format": audio_format},
        output_data={"status": response.get("status") if isinstance(response, dict) else None},
        usage=usage,
        cost=cost,
        metadata={"duration_seconds": round(duration_seconds, 2)}
    )

    return response


@observe(as_type="generation")
def text_translate_provider(message_body, target_language, source_language, voice_provider=None, company_bot=None):
    try:
        if not voice_provider and company_bot:
            voice_provider = get_voice_provider(
                company_bot=company_bot, voice_type=VoiceType.TextToText, source_language=source_language,
                target_language=target_language
            )

        provider_name = voice_provider.provider
        char_count = len(message_body or "")

        if provider_name == VoiceProvider.AI4Bharat:
            response = call_ai4bharat_translation_api(
                source_language=source_language, target_language=target_language, message_body=message_body,
                voice_provider=voice_provider
            )
        elif provider_name == VoiceProvider.GOOGLE:
            secret = settings.SECRETS
            response = translate_text(
                project_id=secret.get('project_id'), text=message_body,
                source_language_code=LanguageMapping.get_google_translate_language(source_language),
                target_language_code=LanguageMapping.get_google_translate_language(target_language),
                voice_provider=voice_provider
            )
        elif provider_name == VoiceProvider.SARVAM:
            service = SarvamLanguageService()
            response = service.translate(
                input_text=message_body,
                source_lang=LanguageMapping.get_sarvam_language(source_language),
                target_lang=LanguageMapping.get_sarvam_language(target_language),
                voice_provider=voice_provider
            )
        elif provider_name == VoiceProvider.CUSTOM_LLM:
            other = getattr(voice_provider, "other_params", {}) or {}
            route = other.get('route', "/transliterate_text")
            company_bot = CompanyBot.objects.filter(route=route).first()
            response = handle_custom_translation(
                message_body=message_body, source_language=LanguageMapping.get_mapped_language(source_language),
                target_language=LanguageMapping.get_mapped_language(target_language), company_bot=company_bot
            )
        elif provider_name == VoiceProvider.SHIKSHALOKAM:
            response = sl_translate(
                message_body=message_body, source_language=source_language,
                target_language=target_language, voice_provider=voice_provider
            )
        else:
            return {
                'status': 500,
                'content': "No provider found!"
            }

        pricing = _get_pricing(provider_name, "translate")
        cost = char_count * pricing["rate"]

        _log_generation(
            name=f"translate-{provider_name.lower()}",
            model=f"{provider_name.lower()}-translate",
            input_data={
                "source_language": source_language, "target_language": target_language,
                "char_count": char_count
            },
            output_data={"status": response.get("status") if isinstance(response, dict) else None},
            usage={"input": char_count, "unit": "characters"},
            cost=cost
        )

        return response
    except Exception as e:
        try:
            langfuse_context.score_current_observation(
                name="translation_error",
                value=0,
                data_type="BOOLEAN",
                comment=str(e)[:200]
            )
        except Exception as se:
            logger.error(f"Langfuse score logging failed: {se}")
        return {
            'status': 500,
            'content': str(e)
        }
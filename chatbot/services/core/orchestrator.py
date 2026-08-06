import traceback
import logging
from .base_service import BaseChatService
from .prompt_builder import PromptBuilder
from .message_handler import MessageHandler
from chatbot.celery_tasks.handle_message import translate_and_send_message

logger = logging.getLogger('django')

from langfuse import get_client
langfuse_context = get_client()

class ChatOrchestrator:
    """Main orchestrator for chat processing"""

    def __init__(self, bot_strategy):
        self.bot_strategy = bot_strategy
        self.base_service = BaseChatService()
        self.prompt_builder = PromptBuilder()
        self.message_handler = MessageHandler()

    def process_chat_request(self, channel_name, session_id, profile_id, language):
        """Main processing method"""
        try:
            with langfuse_context.start_as_current_observation(
                as_type="span", name="get_session_data"
            ) as span:
                session_data = self.base_service.get_session_data(
                    session_id=session_id, profile_id=profile_id, bot_route=self.bot_strategy.get_route()
                )
                span.update(output={
                    "has_profile": session_data.get('profile') is not None,
                    "chat_count": len(session_data.get('company_chats', []))
                })

            with langfuse_context.start_as_current_observation(
                as_type="span", name="get_vernacular_and_profile_info"
            ) as span:
                bot_vernacular, intro_mssg = self.base_service.get_bot_vernacular_and_intro(
                    company_bot=session_data['company_bot'], profile=session_data['profile']
                )
                other_info = self.base_service.get_user_profile_info(profile=session_data['profile'])
                span.update(output={"intro_mssg_present": bool(intro_mssg)})

            with langfuse_context.start_as_current_observation(
                as_type="span", name="prepare_messages"
            ) as span:
                messages = self.message_handler.prepare_messages(
                    company_bot=session_data['company_bot'], company_chats=session_data['company_chats'],
                    intro_mssg=intro_mssg, other_info=other_info
                )
                span.update(output={"message_count": len(messages)})

            with langfuse_context.start_as_current_observation(
                as_type="span", name="bot_strategy_process_session"
            ) as span:
                session_result = self.bot_strategy.process_session(
                    session_data, intro_mssg=intro_mssg, other_info=other_info, messages=messages
                )
                span.update(output={
                    "has_error": bool(session_result.get('error')),
                    "state_machine": session_result.get('state_machine')
                })

            if session_result.get('error'):
                return self._handle_error_response(
                    error_msg=session_result['error'], channel_name=channel_name, language=language,
                    chat_session=session_data['chat_session'], company_bot=session_data['company_bot']
                )

            state_machine = session_result.get('state_machine', None)

            with langfuse_context.start_as_current_observation(
                as_type="span", name="filter_and_prepare_temp_messages"
            ) as span:
                temp_company_chats = self.message_handler.get_filtered_chats(
                    session_id=session_id, state_machine=state_machine,
                    company_chats=session_data['company_chats']
                )
                temp_messages = self.message_handler.prepare_messages(
                    company_bot=session_data['company_bot'], company_chats=temp_company_chats,
                    intro_mssg=intro_mssg, other_info=other_info
                )
                span.update(output={"temp_message_count": len(temp_messages)})

            with langfuse_context.start_as_current_observation(
                as_type="span", name="build_system_prompt"
            ) as span:
                prompt_to_use = self.prompt_builder.build_system_prompt(
                    company_bot=session_data['company_bot'], state_machine=state_machine
                )

            response_params = {
                'system_prompt': prompt_to_use,
                'messages': messages,
                'company_bot': session_data['company_bot'],
                'session_id': session_id,
                'channel_name': channel_name,
                'language': language,
                'profile_id': profile_id,
                'temp_messages': temp_messages,
                'intro_mssg': intro_mssg,
            }

            if hasattr(self.bot_strategy, 'get_route') and 'oneshot' in self.bot_strategy.get_route():
                response_params['remaining_stages'] = session_result.get('remaining_stages', [])

            with langfuse_context.start_as_current_observation(
                as_type="span", name="bot_strategy_get_response"
            ) as span:
                response = self.bot_strategy.get_response(**response_params)
                span.update(output={"response_present": response is not None})

            logger.info('Bot response: %s', response)
            return response

        except Exception as e:
            logger.error('Error in chat processing: %s', e, exc_info=True)
            traceback.print_exc()
            langfuse_context.score_current_trace(
                name="pipeline_error",
                value=0,
                data_type="BOOLEAN",
                comment=str(e)[:200]
            )
            return None

    def _handle_error_response(self, error_msg, channel_name, language, chat_session, company_bot):
        """Handle error responses"""
        logger.info(f"Sending error message: {error_msg}")
        return translate_and_send_message(
            accumulated_message=error_msg, current_channel_name=channel_name, finish_reason="stop",
            current_step_number=chat_session.current_step, route=language, company_bot=company_bot
        )

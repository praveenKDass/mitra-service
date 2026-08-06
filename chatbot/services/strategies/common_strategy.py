from chatbot.services.strategies.base_strategy import BotStrategy
from langfuse import get_client
langfuse_context = get_client()


class CommonBotStrategy(BotStrategy):
    """Common Strategy for bot functionality"""

    def get_default_route(self):
        return ''

    def get_handler_type(self):
        return 'common'

    def process_session(self, session_data, **kwargs):
        """Handle common session processing"""
        chat_session = session_data['chat_session']
        company_bot = session_data['company_bot']

        with langfuse_context.start_as_current_observation(
            as_type="span", name="lookup_state_machine",
            input={"current_step": chat_session.current_step}
        ) as span:
            try:
                from chatbot.models.company_models import CompanyStateMachine
                state_machine = CompanyStateMachine.objects.filter(
                    company_bot=company_bot, step=chat_session.current_step
                ).first()
                span.update(output={"state_machine_found": state_machine is not None})
                return {'state_machine': state_machine}
            except Exception as e:
                span.update(output={"error": str(e)})
                return {'error': f"State machine error: {e}"}

    def get_response(self, **kwargs):
        """Get guided guest bot response using handler"""
        with langfuse_context.start_as_current_observation(
            as_type="span", name="common_strategy_handle_response"
        ) as span:
            response = self.response_handler.handle_response(**kwargs)
            span.update(output={"response_present": response is not None})
            return response
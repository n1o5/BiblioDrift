from flask import Blueprint, request, jsonify
from sqlalchemy.exc import SQLAlchemyError
import logging
from app.extensions import csrf
from core.rate_limit.limiter import rate_limit
from core.validators.validators import (
    validate_request, AnalyzeMoodRequest,
    MoodTagsRequest, MoodSearchRequest
)
from core.responses.error_responses import (
    success_response, not_found_error, internal_error,
    service_unavailable_error, handle_exception
)
from core.exceptions.exceptions import (
    LLMCircuitBreakerOpenError, AIServiceException,
    ValidationException, InvalidInputError
)
from ai_service import get_ai_recommendations, get_book_mood_tags_safe
import logging

logger = logging.getLogger(__name__)
mood_bp = Blueprint('mood', __name__, url_prefix='/api/v1')
csrf.exempt(mood_bp)

# mood analysis availability flag
try:
    from mood_analysis.ai_service_enhanced import AIBookService
    ai_service = AIBookService()
    MOOD_ANALYSIS_AVAILABLE = True
except ImportError:
    MOOD_ANALYSIS_AVAILABLE = False


@mood_bp.route('/analyze-mood', methods=['POST'])
@rate_limit('analyze_mood')
def handle_analyze_mood():
    """Analyze book mood using GoodReads reviews."""
    if not MOOD_ANALYSIS_AVAILABLE:
        return service_unavailable_error("Mood analysis not available - missing dependencies")
    
    try:
        data = request.get_json()
        
        is_valid, validated_data = validate_request(AnalyzeMoodRequest, data)
        if not is_valid:
            return jsonify(validated_data), 400
        
        title = validated_data.title
        author = validated_data.author
        
        mood_analysis = ai_service.analyze_book_mood(title, author)
        
        if mood_analysis:
            return success_response(data={"mood_analysis": mood_analysis})
        else:
            return not_found_error("Mood analysis for this book")
            
    except Exception as e:
        logger.error(f"Error in handle_analyze_mood: {str(e)}", exc_info=True)
        return internal_error(str(e))


@mood_bp.route('/mood-tags', methods=['POST'])
@rate_limit('mood_tags')
def handle_mood_tags():
    """Get mood tags for a book."""
    from core.exceptions.exceptions import (
        LLMCircuitBreakerOpenError, AIServiceException, 
        ValidationException, InvalidInputError
    )
    from core.responses.error_responses import handle_exception
    
    try:
        data = request.get_json()
        
        is_valid, validated_data = validate_request(MoodTagsRequest, data)
        if not is_valid:
            return jsonify(validated_data), 400
        
        title = validated_data.title
        author = validated_data.author
        
        mood_tags = get_book_mood_tags_safe(title, author)
        return success_response(
            data={"mood_tags": mood_tags}
        )
        
    except (LLMCircuitBreakerOpenError, AIServiceException) as e:
        logger.error(f"AI service error in handle_mood_tags: {e}", exc_info=True)
        return handle_exception(e, "handle_mood_tags")
    except (ValidationException, InvalidInputError) as e:
        logger.warning(f"Validation error in handle_mood_tags: {e}")
        return handle_exception(e, "handle_mood_tags")
    except Exception as e:
        logger.error(f"Unexpected error in handle_mood_tags: {type(e).__name__}: {e}", exc_info=True)
        return handle_exception(e, "handle_mood_tags")


@mood_bp.route('/mood-search', methods=['POST'])
@rate_limit('mood_search')
def handle_mood_search():
    """Search for books based on mood/vibe with improved query parsing."""
    from core.exceptions.exceptions import (
        LLMCircuitBreakerOpenError, AIServiceException,
        ValidationException, InvalidInputError
    )
    from core.responses.error_responses import handle_exception
    
    try:
        data = request.get_json()
        
        is_valid, validated_data = validate_request(MoodSearchRequest, data)
        if not is_valid:
            return jsonify(validated_data), 400
        
        mood_query = validated_data.query
        
        # Try to use enhanced mood parsing if available
        try:
            from mood_analysis.mood_query_parser import parse_mood_query, get_recommendation_prompt
            parsed_query = parse_mood_query(mood_query)
            enhanced_prompt = get_recommendation_prompt(mood_query)
            
            logger.info(f"Parsed mood query: {parsed_query.to_dict()}")
            
            # Use enhanced prompt for recommendations
            recommendations = get_ai_recommendations(enhanced_prompt)
            
            return success_response(
                data={
                    "recommendations": recommendations,
                    "query": mood_query,
                    "parsed_mood": parsed_query.to_dict()
                }
            )
        except ImportError:
            # Fallback to basic recommendations if mood parser not available
            logger.info("Mood query parser not available, using basic recommendations")
            recommendations = get_ai_recommendations(mood_query)
            return success_response(
                data={
                    "recommendations": recommendations,
                    "query": mood_query
                }
            )
        
    except SQLAlchemyError as e:
        logger.error(f"Database error searching mood: {e}")
        return internal_error("A database error occurred during search.")
    except Exception as e:
        logger.error(f"Unexpected error searching mood: {e}")
        return internal_error(str(e))

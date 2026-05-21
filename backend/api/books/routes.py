from flask import Blueprint, request, jsonify
from sqlalchemy.exc import SQLAlchemyError
from datetime import datetime, timezone
import logging
import os
import requests
from app.extensions import csrf
from core.rate_limit.limiter import rate_limit
from core.validators.validators import (
    validate_request, CategoryBooksRequest,
    GenerateNoteRequest, ChatRequest
)
from core.responses.error_responses import (
    success_response, internal_error,
    service_unavailable_error, handle_exception
)
from core.exceptions.exceptions import (
    LLMCircuitBreakerOpenError, AIServiceException,
    ValidationException, InvalidInputError,
    DatabaseQueryError, DatabaseIntegrityError
)
from ai_service import (
    get_ai_recommendations, get_category_books,
    generate_book_note, generate_chat_response
)
from models import db, BookNote

logger = logging.getLogger(__name__)
books_bp = Blueprint('books', __name__, url_prefix='/api/v1')
csrf.exempt(books_bp)


@books_bp.route('/category-books', methods=['POST'])
@rate_limit('category_books')
def handle_category_books():
    """
    Return AI-generated, category-specific book recommendations.

    Fix for: all shelf categories displaying the same default books.

    Each category sends its name + vibe description. The LLM returns a list
    of real book titles and authors specific to that vibe. The frontend uses
    these titles to query the Google Books API for actual cover images and
    metadata — ensuring each shelf displays genuinely different, relevant books.

    Request body:
        {
            "category": "Rainy Evening Reads",
            "vibe_description": "quiet and melancholy, best read on grey afternoons",
            "count": 5
        }

    Response:
        {
            "success": true,
            "data": {
                "category": "Rainy Evening Reads",
                "books": [ ... ]
            }
        }
    """
    try:
        data = request.get_json()

        is_valid, validated_data = validate_request(CategoryBooksRequest, data)
        if not is_valid:
            return jsonify(validated_data), 400

        books = get_category_books(
            category=validated_data.category,
            vibe_description=validated_data.vibe_description,
            count=validated_data.count,
        )

        if not books:
            return service_unavailable_error(
                "Could not generate book recommendations right now. Please try again shortly."
            )

        return success_response(
            data={
                "category": validated_data.category,
                "books": books,
            }
        )

    except Exception as e:
        logger.error(f"Error in handle_category_books: {str(e)}", exc_info=True)
        return internal_error(str(e))


@books_bp.route('/generate-note', methods=['POST'])
@rate_limit('generate_note')
def handle_generate_note():
    """Generate AI-powered book recommendation with vibe support."""
    from core.exceptions.exceptions import (
        LLMCircuitBreakerOpenError, AIServiceException,
        DatabaseQueryError, DatabaseIntegrityError,
        ValidationException, InvalidInputError
    )
    from core.responses.error_responses import handle_exception
    
    try:
        data = request.get_json()
        
        is_valid, validated_data = validate_request(GenerateNoteRequest, data)
        if not is_valid:
            return jsonify(validated_data), 400
        
        description = validated_data.description
        title = validated_data.title
        author = validated_data.author
        vibe = getattr(validated_data, 'vibe', 'cozy discovery')
        
        # Check cache
        cached_note = BookNote.query.filter_by(book_title=title, book_author=author).first()
        if cached_note:
            logger.debug(f"Cache hit for {title} by {author}")
            return success_response(data={"blurb": cached_note.content})
        
        # Generate AI recommendation with vibe context
        recommendation = generate_book_note(description, title, author, vibe)
        
        try:
            if recommendation and isinstance(recommendation, dict):
                blurb_content = recommendation.get('blurb', str(recommendation))
                new_note = BookNote(book_title=title, book_author=author, content=blurb_content)
                db.session.add(new_note)
                db.session.commit()
        except SQLAlchemyError as e:
            logger.error(f"Database error caching note: {e}")
            db.session.rollback()
        except Exception as e:
            logger.error(f"Unexpected error caching note: {e}")
            db.session.rollback()

        return success_response(data=recommendation)
        
    except (LLMCircuitBreakerOpenError, AIServiceException) as e:
        logger.error(f"AI service error in handle_generate_note: {e}", exc_info=True)
        return handle_exception(e, "handle_generate_note")
    except (ValidationException, InvalidInputError) as e:
        logger.warning(f"Validation error in handle_generate_note: {e}")
        return handle_exception(e, "handle_generate_note")
    except Exception as e:
        logger.error(f"Unexpected error in handle_generate_note: {type(e).__name__}: {e}", exc_info=True)
        return handle_exception(e, "handle_generate_note")


@books_bp.route('/chat', methods=['POST'])
@rate_limit('chat')
def handle_chat():
    """Handle chat messages and generate bookseller responses."""
    from core.exceptions.exceptions import (
        LLMCircuitBreakerOpenError, AIServiceException,
        ValidationException, InvalidInputError
    )
    from core.responses.error_responses import handle_exception
    
    try:
        data = request.get_json()
        
        is_valid, validated_data = validate_request(ChatRequest, data)
        if not is_valid:
            return jsonify(validated_data), 400
        
        user_message = validated_data.message
        conversation_history = validated_data.history or []
        
        validated_history = []
        for msg in conversation_history:
            if hasattr(msg, 'dict'):
                validated_history.append(msg.dict())
            else:
                validated_history.append(msg)
        
        # Generate contextual response based on conversation history
        response = generate_chat_response(user_message, validated_history)
        
        # Try to get book recommendations based on the message
        recommendations = get_ai_recommendations(user_message)
        
        # TIMESTAMP STANDARDIZATION
        return success_response(
            data={
                "response": response,
                "recommendations": recommendations,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
        )
        
    except (LLMCircuitBreakerOpenError, AIServiceException) as e:
        logger.error(f"AI service error in handle_chat: {e}", exc_info=True)
        return handle_exception(e, "handle_chat")
    except (ValidationException, InvalidInputError) as e:
        logger.warning(f"Validation error in handle_chat: {e}")
        return handle_exception(e, "handle_chat")
    except Exception as e:
        logger.error(f"Unexpected error in handle_chat: {type(e).__name__}: {e}", exc_info=True)
        return handle_exception(e, "handle_chat")


@books_bp.route('/books', methods=['GET'])
def get_books():
    query = request.args.get('q')
    max_results = request.args.get('maxResults', 10)

    API_KEY = os.getenv("GOOGLE_BOOKS_API_KEY")
    url = f"https://www.googleapis.com/books/v1/volumes?q={query}&maxResults={max_results}&key={API_KEY}"

    try:
        response = requests.get(url)
        data = response.json()
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": "Failed to fetch books"}), 500

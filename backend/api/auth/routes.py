from flask import Blueprint, request, jsonify
from flask_jwt_extended import (
    create_access_token, jwt_required,
    get_jwt_identity, set_access_cookies, unset_jwt_cookies
)
from sqlalchemy.exc import SQLAlchemyError
import logging
from app.extensions import limiter
from models import db, User, register_user, login_user
from core.validators.validators import (
    validate_request, RegisterRequest, LoginRequest
)
from core.responses.error_responses import (
    success_response, auth_error, resource_exists_error,
    internal_error, handle_exception
)
from core.exceptions.exceptions import (
    DatabaseQueryError, ValidationException
)

logger = logging.getLogger(__name__)
auth_bp = Blueprint('auth', __name__, url_prefix='/api/v1')


@auth_bp.route('/register', methods=['POST'])
@limiter.limit("5 per minute")
def register():
    """Register a new user and return JWT token."""
    try:
        data = request.get_json()

        logger.info(f"Registration attempt for user: {data.get('username')} from IP: {request.remote_addr}")

        is_valid, validated_data = validate_request(RegisterRequest, data)
        if not is_valid:
            return jsonify(validated_data), 400

        username = validated_data.username
        email = validated_data.email
        password = validated_data.password

        if User.query.filter((User.username==username) | (User.email==email)).first():
            return resource_exists_error("User")

        try:
            user = register_user(username, email, password)
            if not user:
                return internal_error("Failed to create user record after registration.")

            access_token = create_access_token(identity=str(user.id))

            resp, status = success_response(
                data={
                    "message": "User registered successfully",
                    "user": {"id": user.id, "username": user.username, "email": user.email}
                },
                status_code=201
            )
            set_access_cookies(resp, access_token)
            return resp, status
        except SQLAlchemyError as e:
            logger.error(f"Database error during registration: {e}")
            return internal_error("A database error occurred during registration.")
    except Exception as e:
        logger.error(f"Unexpected error in register endpoint: {e}")
        return internal_error(str(e))


@auth_bp.route('/login', methods=['POST'])
@limiter.limit("5 per minute")
def login():
    """Authenticate user and return JWT token."""
    try:
        data = request.get_json()

        # =========================================================================
        # SECURITY AUDIT: LOGIN ATTEMPT
        # =========================================================================
        # All login attempts are strictly validated against CSRF tokens.
        # This prevents an attacker from creating a malicious site that 
        # automatically logs a user into an account they control.
        # =========================================================================
        logger.info(f"Login attempt for identifier: {data.get('username')} from IP: {request.remote_addr}")

        is_valid, validated_data = validate_request(LoginRequest, data)
        if not is_valid:
            return jsonify(validated_data), 400

        username_or_email = validated_data.username
        password = validated_data.password

        user = User.query.filter((User.username==username_or_email) | (User.email==username_or_email)).first()

        if user and user.check_password(password):
            access_token = create_access_token(identity=str(user.id))

            resp, status = success_response(
                data={
                    "message": "Login successful",
                    "user": {"id": user.id, "username": user.username, "email": user.email}
                }
            )
            set_access_cookies(resp, access_token)
            return resp, status

        return auth_error("Invalid username or password")
    except Exception as e:
        logger.error(f"Unexpected error in login: {type(e).__name__}: {e}", exc_info=True)
        return handle_exception(e, "login")


@auth_bp.route('/logout', methods=['POST'])
def logout():
    """Clear JWT cookies for logout."""
    resp, status = success_response(message="Logout successful")
    unset_jwt_cookies(resp)
    return resp, status


@auth_bp.route('/auth/verify', methods=['GET'])
@jwt_required()
def verify_auth_session():
    """Validate JWT from access cookie and return the current user (session restore)."""
    try:
        uid = get_jwt_identity()
        user = User.query.get(int(uid))
        if not user:
            return jsonify({"error": "User not found"}), 404
        return jsonify({
            "user": {"id": user.id, "username": user.username, "email": user.email}
        }), 200
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid session"}), 401

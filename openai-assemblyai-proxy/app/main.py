import os
import logging
from fastapi import FastAPI, HTTPException, Request, File, UploadFile, Form
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
from .models import (
    OpenAITranscriptionRequest, 
    OpenAITranscriptionResponse, 
    AssemblyAITranscriptionRequest,
    ErrorResponse,
    HealthResponse
)
from .assemblyai_client import AssemblyAIClient
from .utils import (
    setup_logging,
    map_language_code,
    map_openai_model_to_speech_model,
    parse_word_boost,
    format_openai_error,
    convert_assemblyai_to_openai_response,
    validate_audio_url,
    get_current_timestamp,
    parse_prompt_for_speaker_diarization,
    parse_prompt_for_config
)


# Setup logging
logger = setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager"""
    logger.info("Starting OpenAI to AssemblyAI Proxy API")
    logger.info("API started successfully - AssemblyAI API key will be taken from client requests")
    yield
    logger.info("API shutting down")


# Initialize FastAPI app
app = FastAPI(
    title="OpenAI to AssemblyAI Proxy API",
    description="Proxy API that makes AssemblyAI compatible with OpenAI Python SDK",
    version="1.0.0",
    lifespan=lifespan
)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Global exception handler"""
    logger.error(f"Unhandled exception: {str(exc)}")
    return JSONResponse(
        status_code=500,
        content=format_openai_error(
            message="Internal server error",
            error_type="api_error"
        )
    )


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint for Cloud Run"""
    return HealthResponse(
        status="healthy",
        timestamp=get_current_timestamp()
    )


@app.post("/v1/audio/transcriptions")
async def create_transcription(
    request: Request,
    file: UploadFile = File(None),
    model: str = Form("whisper-1"),
    language: str = Form(None),
    prompt: str = Form(None),
    response_format: str = Form("json"),
    temperature: float = Form(0.0),
    audio_url: str = Form(None)
):
    """
    Create a transcription using AssemblyAI API with OpenAI-compatible interface
    """
    
    try:
        # Extract API key from Authorization header
        auth_header = request.headers.get("authorization")
        if not auth_header:
            raise HTTPException(
                status_code=401,
                detail=format_openai_error(
                    "No authorization header provided",
                    "invalid_request_error",
                    "missing_authorization"
                )
            )
        
        # Extract the API key (format: "Bearer sk-...")
        api_key = None
        if auth_header.startswith("Bearer "):
            api_key = auth_header[7:]  # Remove "Bearer " prefix
        else:
            api_key = auth_header  # Direct API key
        
        if not api_key:
            raise HTTPException(
                status_code=401,
                detail=format_openai_error(
                    "Invalid authorization header format",
                    "invalid_request_error",
                    "invalid_authorization"
                )
            )
        # Determine if we have a file upload or URL
        final_audio_url = None
        
        if file:
            logger.info(f"Received transcription request with file upload: {file.filename}")
            
            # Initialize AssemblyAI client for file upload
            try:
                client = AssemblyAIClient(api_key=api_key)
            except ValueError as e:
                logger.error(f"Failed to initialize AssemblyAI client: {str(e)}")
                raise HTTPException(
                    status_code=500,
                    detail=format_openai_error(
                        message="AssemblyAI API key not configured",
                        error_type="api_error",
                        code="missing_api_key"
                    )
                )
            
            # Read and upload file
            try:
                file_content = await file.read()
                final_audio_url = client.upload_file(file_content, file.filename)
                logger.info(f"File uploaded successfully: {final_audio_url}")
            except Exception as e:
                logger.error(f"Failed to upload file: {str(e)}")
                raise HTTPException(
                    status_code=400,
                    detail=format_openai_error(
                        message=f"Failed to upload file: {str(e)}",
                        error_type="invalid_request_error",
                        code="file_upload_failed"
                    )
                )
        
        elif audio_url:
            logger.info(f"Received transcription request for audio URL: {audio_url}")
            
            # Validate audio URL
            if not validate_audio_url(audio_url):
                logger.warning(f"Invalid audio URL: {audio_url}")
                raise HTTPException(
                    status_code=400,
                    detail=format_openai_error(
                        message="Invalid audio URL format or unsupported audio type",
                        error_type="invalid_request_error",
                        code="invalid_audio_url"
                    )
                )
            final_audio_url = audio_url
        
        else:
            logger.warning("No file or audio_url provided")
            raise HTTPException(
                status_code=400,
                detail=format_openai_error(
                    message="Either 'file' or 'audio_url' parameter is required",
                    error_type="invalid_request_error",
                    code="missing_audio_input"
                )
            )
        
        # Log ignored parameters
        if temperature != 0.0:
            logger.info(f"Temperature parameter '{temperature}' ignored")
        
        # Parse prompt for config parameters (JSON or legacy patterns)
        config_dict, cleaned_prompt = parse_prompt_for_config(prompt)
        logger.info(f"Original prompt: '{prompt}'")
        logger.info(f"Parsed config: {config_dict}")
        logger.info(f"Cleaned prompt: '{cleaned_prompt}'")
        
        # Extract speaker diarization for backward compatibility logging
        speaker_diarization = config_dict.get("speaker_labels", False)
        logger.info(f"Speaker diarization enabled: {speaker_diarization}")
        
        # Map OpenAI parameters to AssemblyAI format
        language_code = map_language_code(language)
        speech_model = map_openai_model_to_speech_model(model)
        word_boost = parse_word_boost(cleaned_prompt)
        
        # Validate model parameter
        if model and speech_model is None:
            logger.warning(f"Invalid model parameter: '{model}'. Valid values are: best, slam-1, universal")
            raise HTTPException(
                status_code=400,
                detail=format_openai_error(
                    message=f"Invalid model '{model}'. Valid AssemblyAI speech models are: best, slam-1, universal",
                    error_type="invalid_request_error",
                    code="invalid_model"
                )
            )
        
        # Log model mapping
        if model and speech_model:
            logger.info(f"Using AssemblyAI speech_model: '{speech_model}'")
        
        # Create base AssemblyAI request parameters
        base_params = {
            "audio_url": final_audio_url,
            "language_code": language_code,
            "speech_model": speech_model,
            "word_boost": word_boost,
            "speaker_labels": speaker_diarization,
            "punctuate": True,
            "format_text": True
        }
        
        # Merge with config parameters from prompt (config takes precedence)
        merged_params = {**base_params, **config_dict}
        
        # Remove None values to avoid sending them to AssemblyAI
        merged_params = {k: v for k, v in merged_params.items() if v is not None}
        
        logger.info(f"Final AssemblyAI request parameters: {merged_params}")
        
        # Create AssemblyAI request
        assemblyai_request = AssemblyAITranscriptionRequest(**merged_params)
        
        # Initialize AssemblyAI client (if not already initialized for file upload)
        if not file:
            try:
                client = AssemblyAIClient(api_key=api_key)
            except ValueError as e:
                logger.error(f"Failed to initialize AssemblyAI client: {str(e)}")
                raise HTTPException(
                    status_code=500,
                    detail=format_openai_error(
                        message="AssemblyAI API key not configured",
                        error_type="api_error",
                        code="missing_api_key"
                    )
                )
        
        # Perform transcription
        try:
            assemblyai_response = client.transcribe(assemblyai_request)
            logger.info(f"Transcription completed successfully")
            
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Transcription failed: {error_msg}")
            
            # Determine error type and status code
            if "timeout" in error_msg.lower():
                status_code = 408
                error_type = "timeout_error"
                code = "transcription_timeout"
            elif "invalid" in error_msg.lower() or "bad request" in error_msg.lower():
                status_code = 400
                error_type = "invalid_request_error"
                code = "invalid_audio"
            elif "unauthorized" in error_msg.lower():
                status_code = 401
                error_type = "authentication_error"
                code = "invalid_api_key"
            elif "not found" in error_msg.lower():
                status_code = 404
                error_type = "not_found_error"
                code = "audio_not_found"
            else:
                status_code = 500
                error_type = "api_error"
                code = "transcription_failed"
            
            raise HTTPException(
                status_code=status_code,
                detail=format_openai_error(
                    message=error_msg,
                    error_type=error_type,
                    code=code
                )
            )
        
        # Convert response to OpenAI format
        openai_response = convert_assemblyai_to_openai_response(
            assemblyai_response, 
            response_format
        )
        
        # Handle text response format
        if response_format == "text":
            return openai_response
        
        logger.info("Transcription request completed successfully")
        return openai_response
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error in transcription endpoint: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=format_openai_error(
                message="Internal server error",
                error_type="api_error"
            )
        )


if __name__ == "__main__":
    import uvicorn
    
    port = int(os.getenv("PORT", "8080"))
    log_level = os.getenv("LOG_LEVEL", "info").lower()
    
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=port,
        log_level=log_level,
        reload=False
    )

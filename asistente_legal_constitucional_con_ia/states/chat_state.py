import asyncio
import json
import logging
import os
import tempfile
import time
from typing import TypedDict, List, Dict, Any
import pytesseract
import reflex as rx
from dotenv import load_dotenv
from openai import APIError, OpenAI
from pdf2image import convert_from_bytes

from asistente_legal_constitucional_con_ia.util.scraper import (
    scrape_proyectos_recientes_camara,
)
from asistente_legal_constitucional_con_ia.util.text_extraction import \
    extract_text_from_bytes
from asistente_legal_constitucional_con_ia.util.tools import \
    buscar_documento_legal

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("asistente_legal")

load_dotenv()

# Define la herramienta para que el Asistente la entienda
TOOLS_DEFINITION = [
    {
        "type": "function",
        "function": {
            "name": "buscar_documento_legal",
            "description": "Herramienta de búsqueda avanzada para encontrar documentos legales colombianos (leyes, sentencias, gacetas) aplicando la estrategia más adecuada para cada tipo.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "La consulta de búsqueda específica. Para Gacetas, usar solo el número y año, ej: '758 de 2017'. Para Sentencias, el identificador completo, ej: 'Sentencia C-123 de 2023'. Para Leyes, el número y año, ej: 'Ley 1437 de 2011'."
                    },
                    "tipo_documento": {
                        "type": "string",
                        "description": "El tipo de documento legal a buscar. Debe ser uno de: 'gaceta', 'sentencia', 'ley'.",
                        "enum": ["gaceta", "sentencia", "ley"]
                    },
                    "sitio_preferido": {
                        "type": "string",
                        "description": "Opcional. Usar para priorizar dominios de alta autoridad. Ej: 'corteconstitucional.gov.co' para sentencias o 'suin-juriscol.gov.co' para leyes. NO usar para gacetas."
                    }
                },
                "required": ["query", "tipo_documento"],
            },
        },
    }
]
AVAILABLE_TOOLS = {
    "buscar_documento_legal": buscar_documento_legal,
}


class Message(TypedDict):
    role: str
    content: str


class FileInfo(TypedDict):
    file_id: str
    filename: str
    uploaded_at: float 


class ChatState(rx.State):
    """Manages the chat interface, file uploads, and AI interaction."""

    messages: list[Message] = []
    thread_id: str | None = None
    file_info_list: list[FileInfo] = []
    session_files: list[FileInfo] = []  # ← AGREGAR ESTA LÍNEA
    processing: bool = False
    uploading: bool = False
    upload_progress: int = 0
    ocr_progress: str = ""
    proyectos_recientes_df: str = ""
    assistant_id: str = os.getenv("ASSISTANT_ID_CONSTITUCIONAL", "")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    streaming_response: str = ""
    streaming: bool = False
    thinking_seconds: int = 0
    upload_error: str = ""
    focus_chat_input: bool = False
    current_question: str = ""
    chat_history: list = []
    current_answer: str = ""
    error_message: str = ""
    is_uploading: bool = False
    is_performing_ocr: bool = False
    uploaded_file_name: str = ""
    file_context: str = ""
    show_notebook_dialog: bool = False  # Para mostrar diálogo de creación de notebook
    notebook_title: str = ""  # Título del notebook a crear

    @staticmethod
    def get_client(api_key: str):
        if api_key:
            return OpenAI(api_key=api_key)
        return None

    def scroll_to_bottom(self):
        """Scroll agresivo SOLO cuando es necesario"""
        return rx.call_script(
            """
            function forceScrollToBottom() {
                const chatContainer = document.getElementById('chat-messages-container');
                if (chatContainer) {
                    // MÚLTIPLES intentos para garantizar scroll del mensaje usuario
                    chatContainer.scrollTop = chatContainer.scrollHeight;
                    
                    requestAnimationFrame(() => {
                        chatContainer.scrollTop = chatContainer.scrollHeight;
                    });
                    
                    setTimeout(() => {
                        chatContainer.scrollTop = chatContainer.scrollHeight;
                    }, 50);
                    
                    setTimeout(() => {
                        chatContainer.scrollTop = chatContainer.scrollHeight;
                    }, 150);
                    
                    console.log('Scroll aplicado - agresivo');
                }
            }
            
            forceScrollToBottom();
            """
        )

    def focus_input(self):
        """Posicionar el cursor en el input del usuario"""
        return rx.call_script(
            """
            setTimeout(() => {
                const input = document.getElementById('chat-input-box');
                if (input) {
                    input.focus();
                    input.setSelectionRange(input.value.length, input.value.length);
                }
            }, 200);
        """
        )
    
        
    @rx.event
    def set_current_question(self, value: str):
        """Actualiza la pregunta actual"""
        logger.info(f"set_current_question: '{value[:30]}...'")
        self.current_question = value

    @rx.var
    def has_api_keys(self) -> bool:
        return bool(self.assistant_id and self.openai_api_key)

   

    @rx.event(background=True)
    async def upload_timer(self):
        async with self:
            self.thinking_seconds = 0
        while self.uploading:
            await asyncio.sleep(1)
            async with self:
                self.thinking_seconds += 1

    @rx.var
    def proyectos_data(self) -> list[dict]:
        if not self.proyectos_recientes_df:
            return []
        try:
            return json.loads(self.proyectos_recientes_df)
        except json.JSONDecodeError:
            return []

    async def _perform_ocr_with_progress(self, upload_data, file_name):
        """
        Realiza OCR en un archivo PDF, actualizando el progreso por página.
        """
        self.is_performing_ocr = True
        self.ocr_progress = f"Iniciando OCR en '{file_name}'..."
        yield

        ocr_text_parts = []
        try:
            images = await asyncio.to_thread(
                convert_from_bytes, upload_data, dpi=200
            )
            total_pages = len(images)

            for page_num, image in enumerate(images):
                self.ocr_progress = (
                    f"OCR: Pág {page_num + 1}/{total_pages} de '{file_name}'"
                )
                logger.info(f"[UI FEEDBACK] {self.ocr_progress}")
                yield

                text = await asyncio.to_thread(
                    pytesseract.image_to_string, image, lang="spa+eng"
                )
                ocr_text_parts.append(text)

        except Exception as e:
            logger.error(f"Error durante el OCR: {e}")
            self.upload_error = f"Error de OCR en '{file_name}': {e}"
            ocr_text_parts = []  # En caso de error, devolver texto vacío
        finally:
            self.is_performing_ocr = False
            self.ocr_progress = ""
            yield

        # Yield el resultado final en lugar de return
        yield "\n".join(ocr_text_parts)

    @rx.event
    async def handle_upload(self, files: list[rx.UploadFile]):
        logger.info(f"handle_upload: {len(files)} archivos recibidos")
        self.upload_error = ""
        if not files:
            self.upload_error = "No se seleccionaron archivos."
            yield rx.toast.error(self.upload_error)
            return

        self.uploading = True
        self.upload_progress = 0
        yield

        client = self.get_client(self.openai_api_key)
        if not client:
            self.upload_error = "Credenciales de OpenAI no configuradas."
            logger.error(self.upload_error)
            yield rx.toast.error(self.upload_error)
            self.uploading = False
            return

        for i, file in enumerate(files):
            if any(f["filename"] == file.name for f in self.file_info_list):
                self.upload_error = f"El archivo '{file.name}' ya fue subido."
                logger.warning(self.upload_error)
                yield rx.toast.error(self.upload_error)
                continue

            try:
                logger.info(f"Procesando archivo: {file.name}")
                upload_data = await file.read()
                extracted_text = extract_text_from_bytes(
                    upload_data, file.name, skip_ocr=True
                )

                if (file.name.lower().endswith(".pdf") and 
                    (not extracted_text or len(extracted_text.strip()) < 100)):
                    
                    # Llamar a la función separada para procesamiento OCR
                    self.uploading = False
                    yield
                    
                    ocr_result_generator = self._perform_ocr_with_progress(
                        upload_data, file.name
                    )
                    
                    # Iterar sobre el generador y obtener el último valor (el texto)
                    extracted_text = ""
                    async for result in ocr_result_generator:
                        if isinstance(result, str):  # El último yield es el texto
                            extracted_text = result
                        yield  # Propagar los yields intermedios

                    self.uploading = True
                    yield
                    
                    if not extracted_text or not extracted_text.strip():
                        yield rx.toast.error(f"Falló el OCR para '{file.name}'")
                        continue


                if not extracted_text or not extracted_text.strip():
                    self.upload_error = f"No se pudo extraer texto de '{file.name}'."
                    logger.warning(self.upload_error)
                    yield rx.toast.warning(self.upload_error)
                    continue

                original_name_no_ext = os.path.splitext(file.name)[0]
                temp_filename = f"{original_name_no_ext}_processed.txt"
                temp_dir = tempfile.gettempdir()
                tmp_path = os.path.join(temp_dir, temp_filename)
                
                with open(tmp_path, "w", encoding="utf-8") as tmp_file:
                    tmp_file.write(extracted_text)

                try:
                    with open(tmp_path, "rb") as f_obj:
                        response = client.files.create(
                            file=f_obj, purpose="assistants"
                        )

                    self.file_info_list.append(
                        {"file_id": response.id, "filename": file.name, "uploaded_at": time.time()} 
                    )

                    self.session_files.append(
                        {"file_id": response.id, "filename": file.name, "uploaded_at": time.time()}
                    )

                    logger.info(f"'{file.name}' subido con id {response.id}.")
                    self.upload_error = ""
                    yield rx.toast.success(f"'{file.name}' procesado y subido.")
                except APIError as e:
                    self.upload_error = f"Error al subir '{file.name}': {e.message}"
                    logger.error(self.upload_error)
                    yield rx.toast.error(self.upload_error)
                finally:
                    os.remove(tmp_path)

            except Exception as e:
                self.uploading = False
                self.is_performing_ocr = False
                self.ocr_progress = ""
                self.upload_error = f"Error procesando '{file.name}': {e}"
                logger.error(self.upload_error)
                yield rx.toast.error(self.upload_error)

            self.upload_progress = round((i + 1) / len(files) * 100)
            yield

        self.uploading = False
        self.is_performing_ocr = False
        self.ocr_progress = ""
        logger.info("handle_upload: proceso terminado")
        yield

    @rx.event
    def delete_file(self, file_id: str):
        client = self.get_client(self.openai_api_key)
        if not client:
            yield rx.toast.error("Credenciales de OpenAI no configuradas.")
            return

        filename = next(
            (f["filename"] for f in self.file_info_list if f["file_id"] == file_id),
            "archivo",
        )
        try:
            client.files.delete(file_id)
            self.file_info_list = [
                f for f in self.file_info_list if f["file_id"] != file_id
            ]
            self.session_files = [f for f in self.session_files if f["file_id"] != file_id]
            yield rx.toast.success(f"'{filename}' eliminado.")
        except APIError as e:
            yield rx.toast.error(f"Error eliminando '{filename}': {e.message}")

    @rx.event(background=True)
    async def scrape_proyectos(self):
        async with self:
            self.proyectos_recientes_df = ""
        df = scrape_proyectos_recientes_camara(15)
        async with self:
            if df is not None:
                self.proyectos_recientes_df = df.to_json(orient="records")
            else:
                self.proyectos_recientes_df = "[]"
                yield rx.toast.error("No se pudieron obtener los proyectos.")

    @rx.event(background=True)
    async def thinking_timer(self):
        async with self:
            self.thinking_seconds = 0
        logger.info(f"thinking_timer: Iniciado. self.processing={self.processing}")
        
        start_time = time.time()
        max_timeout = 600  # 10 minutos máximo
        
        while self.processing:
            current_time = time.time()
            
            # Timeout de seguridad
            if current_time - start_time > max_timeout:
                logger.warning(f"thinking_timer: Timeout después de {max_timeout}s")
                async with self:
                    self.processing = False
                    self.messages[-1]["content"] = "Error: Tiempo de respuesta agotado."
                break
                
            await asyncio.sleep(1)
            async with self:
                self.thinking_seconds += 1
        
        logger.info(f"thinking_timer: Detenido. self.processing={self.processing}")

    @rx.event
    def send_message(self, form_data: dict):
        user_prompt = self.current_question.strip()
        logger.info(f"send_message: INICIO. prompt='{user_prompt}'")
        if not user_prompt or self.processing:
            logger.warning("Prompt vacío o ya procesando.")
            return

        self.current_question = ""
        yield rx.call_script("document.getElementById('chat-input-box').value = ''")

        if not self.has_api_keys:
            msg = "Las credenciales de OpenAI no están configuradas."
            logger.error(msg)
            return rx.toast.error(msg)

        self.processing = True
        self.streaming = True
        self.streaming_response = ""
        self.thinking_seconds = 0
        # PASO 1: Mostrar mensaje del usuario INMEDIATAMENTE
        self.messages.append({"role": "user", "content": user_prompt})
        yield  # ← CRUCIAL: Actualizar UI con mensaje del usuario
        yield self.scroll_to_bottom()  # ← Posicionar inmediatamente
        
        # PASO 2: Preparar para procesamiento  
        self.processing = True
        self.streaming = True
        self.streaming_response = ""
        self.thinking_seconds = 0
        self.messages.append({"role": "assistant", "content": "Estoy pensando..."})
        yield  # ← Mostrar "Estoy pensando..."
        
        # PASO 3: Iniciar procesamiento (SIN scroll aquí para evitar rebote)
        yield ChatState.thinking_timer
        yield ChatState.generate_response_streaming
        

    @rx.event(background=True)
    async def simple_background_test(self):
        logger.info("simple_background_test: INICIO Y FIN")
        async with self:
            self.messages[-1]["content"] = "Respuesta de prueba"
            self.processing = False
            self.streaming = False
            self.thinking_seconds = 0

    @rx.event(background=True)
    async def generate_response_streaming(self):
        logger.info(f"DEBUG: Estado actual - session_files: {len(self.session_files)}, file_info_list: {len(self.file_info_list)}")
        for fi in self.session_files:
            logger.info(f"DEBUG: Archivo en sesión: {fi['filename']} -> {fi['file_id']}")
        logger.info(f"generate_response_streaming: INICIO. thread_id={self.thread_id}")
        client = self.get_client(self.openai_api_key)

        try:
            last_user_message = next(
                (m["content"] for m in reversed(self.messages) if m["role"] == "user"), None
            )
            if not last_user_message:
                raise ValueError("No se encontró el último mensaje del usuario.")
            
            # ← MOVER AQUÍ: Definir current_files ANTES de usarlo
            current_files = self.session_files[-3:].copy()  # ← Snapshot inmutable

            # SIEMPRE crear thread nuevo si no hay archivos actuales
            if not self.thread_id or (not current_files and self.thread_id):
                thread = await asyncio.to_thread(client.beta.threads.create)
                async with self:
                    self.thread_id = thread.id
                logger.info(f"Thread nuevo creado (limpio): {self.thread_id}")

            # DESPUÉS de línea 461, AGREGAR:
            logger.info(f"DEBUG THREAD - thread_id: {self.thread_id}")

            # Verificar mensajes existentes en el thread
            try:
                existing_messages = await asyncio.to_thread(
                    client.beta.threads.messages.list,
                    thread_id=self.thread_id,
                    limit=5
                )
                logger.info(f"DEBUG THREAD - mensajes existentes: {len(existing_messages.data)}")
                for i, msg in enumerate(existing_messages.data):
                    logger.info(f"DEBUG THREAD mensaje {i}: {msg.role} - {len(msg.attachments)} attachments")
                    for j, att in enumerate(msg.attachments):
                        logger.info(f"DEBUG THREAD attachment {j}: {att.file_id}")
            except Exception as e:
                logger.error(f"Error verificando mensajes del thread: {e}")
            logger.info(f"generate_response_streaming: thread_id={self.thread_id}")

            attachments = [
                {"file_id": fi["file_id"], "tools": [{"type": "file_search"}]}
                for fi in current_files  # ← Usar snapshot
            ]

            # ← AGREGAR DEBUGGING AQUÍ:
            logger.info(f"DEBUG ARCHIVO - session_files: {len(self.session_files)}")
            logger.info(f"DEBUG ARCHIVO - current_files: {len(current_files)}")
            logger.info(f"DEBUG ARCHIVO - attachments: {len(attachments)}")
            for i, fi in enumerate(current_files):
                logger.info(f"DEBUG ARCHIVO {i}: {fi['filename']} -> {fi['file_id']}")

            if not current_files:
                logger.info("DEBUG ARCHIVO - NO HAY ARCHIVOS EN SESIÓN")
            else:
                logger.info(f"DEBUG ARCHIVO - HAY {len(current_files)} ARCHIVOS EN SESIÓN")

            if current_files:
                logger.info(f"DEBUG: Archivos que se van a usar: {[f'{fi['filename']} ({fi['file_id']})' for fi in current_files]}")
            else:
                logger.info("DEBUG: No se enviarán archivos (lista vacía)")

            if current_files:
                file_names = [fi["filename"] for fi in current_files]
                file_list = ", ".join(file_names)
                message_content = f"{last_user_message}\n\n[Archivos adjuntos: {file_list}]"
            else:
                message_content = f"{last_user_message}\n\n[SISTEMA: No hay archivos subidos]"

            await asyncio.to_thread(
                client.beta.threads.messages.create,
                thread_id=self.thread_id,
                role="user",
                content=message_content,
                attachments=attachments,
            )

            tools_for_run = TOOLS_DEFINITION.copy()
            if current_files:  # ← USAR current_files en lugar de attachments
                logger.info(f"Habilitando 'file_search' para {len(current_files)} archivos.")
                tools_for_run.append({"type": "file_search"})
            else:
                logger.info("Sin archivos de sesión: NO habilitando file_search")
                # NO agregamos file_search para forzar que no tenga acceso a archivos

            logger.info(f"DEBUG TOOLS FINAL - tools_for_run: {len(tools_for_run)} herramientas")
            for i, tool in enumerate(tools_for_run):
                if tool.get("type") == "file_search":
                    logger.info(f"DEBUG TOOLS {i}: file_search habilitado")
                elif tool.get("type") == "function":
                    logger.info(f"DEBUG TOOLS {i}: función {tool['function']['name']}")
                else:
                    logger.info(f"DEBUG TOOLS {i}: {tool}")    

            try:
                run_stream = await asyncio.wait_for(
                    asyncio.to_thread(
                        client.beta.threads.runs.create,
                        thread_id=self.thread_id,
                        assistant_id=self.assistant_id,
                        tools=tools_for_run,
                        stream=True,
                    ),
                    timeout=300  # 5 minutos máximo
                )
            except asyncio.TimeoutError:
                logger.error("Timeout creando run de OpenAI")
                async with self:
                    self.messages[-1]["content"] = "Error: La respuesta tardó demasiado."
                    self.processing = False
                    self.streaming = False
                return

            logger.info("generate_response_streaming: Run creado con stream=True.")

            first_chunk_processed = False
            accumulated_response = ""
            accumulated_content = ""
            last_update_time = time.time()

            while True:
                should_break_outer_loop = False
                
                for event in run_stream:
                    if event.event == "thread.message.delta":
                        delta = event.data.delta
                        if delta.content:
                            text_chunk = delta.content[0].text.value
                            if text_chunk:
                                accumulated_content += text_chunk
                                current_time = time.time()
                                
                                # BUFFER INTELIGENTE: actualizar por tiempo O contenido especial
                                should_update = (
                                    len(accumulated_content) >= 2 or                    # Buffer pequeño (5 chars)
                                    "\n" in text_chunk or                              # Inmediato en nueva línea
                                    "." in text_chunk or "!" in text_chunk or "?" in text_chunk or  # Inmediato en puntuación
                                    (current_time - last_update_time) >= 0.05           # Máximo cada 100ms
                                )
                                
                                if should_update:
                                    async with self:
                                        if not first_chunk_processed:
                                            self.messages[-1]["content"] = ""
                                            first_chunk_processed = True
                                        accumulated_response += accumulated_content
                                        self.messages[-1]["content"] = accumulated_response
                                    yield
                                    
                                    # POR: (scroll menos agresivo)
                                    if (len(accumulated_response) % 150 == 0):   
                                        yield self.scroll_to_bottom()
                                    accumulated_content = ""
                                    last_update_time = current_time

                    elif event.event == "thread.run.requires_action":
                        run_id = event.data.id
                        tool_outputs = []

                        async with self:
                            first_query = json.loads(
                                event.data.required_action.submit_tool_outputs.tool_calls[0].function.arguments
                            ).get("query", "...")
                            self.messages[-1]["content"] = f"Buscando: '{first_query}'..."
                        yield
                        
                        for tool_call in event.data.required_action.submit_tool_outputs.tool_calls:
                            function_name = tool_call.function.name
                            arguments = json.loads(tool_call.function.arguments)
                            if function_name in AVAILABLE_TOOLS:
                                try:
                                    # AGREGAR TIMEOUT A HERRAMIENTAS:
                                    output = await asyncio.wait_for(
                                        asyncio.to_thread(AVAILABLE_TOOLS[function_name], **arguments),
                                        timeout=120  # 1 minuto máximo
                                    )
                                except asyncio.TimeoutError:
                                    logger.error(f"Timeout ejecutando herramienta {function_name}")
                                    output = f"Error: La herramienta {function_name} tardó demasiado."
                                except Exception as e:
                                    logger.error(f"Error en herramienta {function_name}: {e}")
                                    output = f"Error ejecutando {function_name}: {str(e)}"
                                
                                tool_outputs.append({"tool_call_id": tool_call.id, "output": output})

                        if tool_outputs:
                            run_stream = await asyncio.to_thread(
                                client.beta.threads.runs.submit_tool_outputs,
                                thread_id=self.thread_id,
                                run_id=run_id,
                                tool_outputs=tool_outputs,
                                stream=True,
                            )
                            break 
                    
                    elif event.event in ["thread.run.completed", "thread.run.failed", "error"]:
                        if event.event != "thread.run.completed":
                            logger.error(f"Stream: Run fallido. Evento: {event.event}")
                            async with self:
                                self.messages[-1]["content"] = "Repite la solicitud por favor."
                        
                        should_break_outer_loop = True
                        break 

                if should_break_outer_loop:
                    break

            # Actualizar cualquier contenido restante del buffer
            if accumulated_content:
                async with self:
                    accumulated_response += accumulated_content
                    self.messages[-1]["content"] = accumulated_response
                yield
                yield self.scroll_to_bottom()

            logger.info("generate_response_streaming: Bucle principal completado.")

        except Exception as e:
            logger.error(f"Error en generate_response_streaming: {e}", exc_info=True)
            async with self:
                self.messages[-1]["content"] = f"Error inesperado: {e}"
        finally:
            logger.info("generate_response_streaming: FIN.")
            async with self:
                self.processing = False
                self.streaming = False
                self.thinking_seconds = 0
                self.focus_chat_input = True
            yield
            yield self.scroll_to_bottom()
            yield self.focus_input()
            yield ChatState.reset_focus_trigger

    @rx.event
    def reset_focus_trigger(self):
        self.focus_chat_input = False

    @rx.event
    def limpiar_chat(self):
        self.cleanup_session_files()
        """Reinicia el estado del chat a sus valores iniciales."""
        self.messages = [
            {
                "role": "assistant",
                "content": "¡Hola! Soy LeyIA, tu Asistente Legal. "
                           "Puedes hacerme una pregunta o subir un "
                           "documento para analizarlo.",
            }
        ]
        self.thread_id = None
        self.file_info_list = []
        #self.session_files = []
        self.processing = False
        self.uploading = False
        self.upload_progress = 0
        self.ocr_progress = ""
        self.streaming_response = ""
        self.streaming = False
        self.thinking_seconds = 0
        self.upload_error = ""
        self.focus_chat_input = False
        self.current_question = ""
        self.chat_history = []
        logger.info("ChatState.limpiar_chat ejecutado.")

    @rx.event
    async def show_create_notebook_dialog(self):
        """Muestra el diálogo para crear notebook."""
        if len(self.messages) < 2:
            return rx.toast.error("Necesitas al menos una conversación para crear un notebook.")
            
        self.show_notebook_dialog = True

    @rx.event
    def hide_create_notebook_dialog(self):
        """Oculta el diálogo de creación de notebook."""
        self.show_notebook_dialog = False

    @rx.event
    async def create_notebook_from_current_chat(self):
        """Crea un notebook a partir de la conversación actual."""
        if not self.notebook_title.strip():
            yield rx.toast.error("El título no puede estar vacío.")
            return
        
        try:
            # Crear el notebook directamente desde este estado sin instanciar otra clase
            title_to_use = self.notebook_title.strip()
            
            # Convertir mensajes del chat a formato notebook
            notebook_content = self._convert_chat_to_notebook(self.messages, title_to_use)
            
            with rx.session() as session:
                from ..models.database import Notebook
                import json
                
                new_notebook = Notebook(
                    title=title_to_use,
                    content=json.dumps(notebook_content),  # Guardar como JSON
                    notebook_type="analysis",
                    workspace_id="public"  # Nuevo esquema sin user_id
                )
                session.add(new_notebook)
                session.commit()
            
            self.show_notebook_dialog = False
            self.notebook_title = ""  # Limpiar el título
            yield rx.toast.success(f"Notebook '{title_to_use}' creado exitosamente.")
            
        except Exception as e:
            yield rx.toast.error(f"Error creando notebook: {str(e)}")

    @rx.event
    async def suggest_notebook_creation(self):
        """Sugiere crear un notebook si la conversación es significativa."""
        if (len(self.messages) >= 4 and  # Al menos 2 intercambios
            not self.processing):
            return rx.toast.info(
                "💡 ¿Quieres guardar esta conversación como notebook?",
                duration=5000
            )

    @rx.event
    def limpiar_chat_y_redirigir(self):
        self.cleanup_session_files()
        self.limpiar_chat()
        return rx.redirect("/")

    @rx.event
    def initialize_chat(self):
        """Añade el mensaje de bienvenida e inicia monitoreo al cargar la página."""
        if not self.messages:
            self.messages = [
                {
                    "role": "assistant",
                    "content": "¡Hola! Soy LeyIA, tu Asistente Legal. "
                            "Puedes hacerme una pregunta o subir un "
                            "documento para analizarlo.",
                }
            ]
        
        # Iniciar monitoreo automático si hay credenciales
        if self.has_api_keys:
            return [
                ChatState.monitor_session_health,
                ChatState.cleanup_by_timestamp
            ]

    @rx.event
    def initialize_chat_simple(self):
        """Inicializa el chat sin métodos de monitoreo que causan recompilaciones."""
        if not self.messages:
            self.messages = [
                {
                    "role": "assistant",
                    "content": "¡Hola! Soy LeyIA, tu Asistente Legal. "
                            "Puedes hacerme una pregunta o subir un "
                            "documento para analizarlo.",
                }
            ]

    @rx.event
    def cleanup_session_files(self):
        """Limpia archivos automáticamente"""
        client = self.get_client(self.openai_api_key)
        if client and self.session_files:
            for file_info in self.session_files:
                try:
                    client.files.delete(file_info["file_id"])
                except APIError:
                    pass  # Archivo ya eliminado
            self.session_files = []

    @rx.event(background=True)
    async def monitor_session_health(self):
        """Monitorea la salud de la sesión y limpia archivos huérfanos"""
        logger.info("Monitor de sesión iniciado")
        while True:
            await asyncio.sleep(300)  # Cada 5 minutos
            
            # Solo verificar si hay archivos y thread_id
            if self.session_files and self.thread_id:
                client = self.get_client(self.openai_api_key)
                if client:
                    try:
                        # Intentar acceder al thread
                        await asyncio.to_thread(
                            client.beta.threads.retrieve, 
                            self.thread_id
                        )
                        logger.info(f"Thread {self.thread_id} activo - {len(self.session_files)} archivos")
                        
                    except APIError as e:
                        if "No thread found" in str(e) or e.status_code == 404:
                            logger.warning(f"Thread {self.thread_id} no encontrado. Limpiando archivos...")
                            await self._cleanup_orphaned_files()
                        else:
                            logger.error(f"Error verificando thread: {e}")
                    
                    except Exception as e:
                        logger.error(f"Error de conexión verificando thread: {e}")
                        # En caso de error de conexión, no limpiar archivos

    async def _cleanup_orphaned_files(self):
        """Limpia archivos cuando la sesión está huérfana"""
        client = self.get_client(self.openai_api_key)
        if client and self.session_files:
            logger.info(f"Limpiando {len(self.session_files)} archivos huérfanos")
            for file_info in self.session_files:
                try:
                    client.files.delete(file_info["file_id"])
                    logger.info(f"Archivo huérfano eliminado: {file_info['filename']}")
                except APIError:
                    pass  # Ya eliminado
            
            # Limpiar estado
            async with self:
                self.session_files = []
                self.thread_id = None
                logger.info("Estado de sesión limpiado por thread huérfano")

    @rx.event(background=True)
    async def cleanup_by_timestamp(self):
        """Respaldo: limpia archivos muy antiguos independiente del thread"""
        logger.info("Monitor de limpieza por timestamp iniciado")
        while True:
            await asyncio.sleep(3600)  # Cada hora
            
            if self.session_files:
                current_time = time.time()
                old_files = []
                
                for file_info in self.session_files:
                    file_age = current_time - file_info.get("uploaded_at", current_time)
                    if file_age > 7200:  # 2 horas
                        old_files.append(file_info)
                
                if old_files:
                    logger.info(f"Encontrados {len(old_files)} archivos antiguos para limpiar")
                    client = self.get_client(self.openai_api_key)
                    if client:
                        for file_info in old_files:
                            try:
                                client.files.delete(file_info["file_id"])
                                logger.info(f"Archivo antiguo eliminado: {file_info['filename']}")
                            except APIError:
                                pass
                        
                        # Remover de la lista
                        async with self:
                            self.session_files = [
                                f for f in self.session_files 
                                if f not in old_files
                            ]

    def _convert_chat_to_notebook(self, chat_messages: List[Dict[str, str]], title: str) -> Dict[str, Any]:
        """Convierte mensajes del chat a formato notebook JSON."""
        from datetime import datetime
        
        cells = []
        
        # Celda de título
        cells.append({
            "cell_type": "markdown",
            "source": [f"# {title}\n\n", f"*Notebook generado automáticamente el {datetime.now().strftime('%d/%m/%Y a las %H:%M')}*\n\n", "---\n\n"]
        })
        
        # Convertir cada intercambio usuario-asistente
        for i, message in enumerate(chat_messages):
            if message["role"] == "user":
                cells.append({
                    "cell_type": "markdown",
                    "source": [f"## 🙋 Consulta {(i//2) + 1}\n\n", f"{message['content']}\n\n"]
                })
            elif message["role"] == "assistant":
                cells.append({
                    "cell_type": "markdown",
                    "source": [f"### 🤖 Respuesta del Asistente\n\n", f"{message['content']}\n\n", "---\n\n"]
                })
        
        return {
            "cells": cells,
            "metadata": {
                "kernelspec": {
                    "display_name": "Legal Analysis",
                    "language": "markdown",
                    "name": "legal_analysis"
                }
            }
        }

    
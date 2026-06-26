# ============================================================
# SISTEMA DE MONITOREO MULTIPARAMÉTRICO (TEMP + PULSO + ECG)
# CON BASE DE DATOS MySQL - TODAS LAS PESTAÑAS
# ============================================================

import sys
import re
import serial
import serial.tools.list_ports
import numpy as np
from scipy.signal import butter, lfilter_zi, lfilter
from collections import deque

# ★ Importaciones para Base de Datos
import mysql.connector
from mysql.connector import Error
from datetime import datetime

import pyqtgraph as pg
from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5 import uic
from PyQt5.QtWidgets import (
    QMainWindow, QApplication, QMessageBox, 
    QVBoxLayout, QInputDialog, QAbstractItemView
)
from PyQt5.QtCore import QTime, QDate


# ═══════════════════════════════════════════════════════════════
# CLASE PARA MANEJAR LA CONEXIÓN A MySQL
# ═══════════════════════════════════════════════════════════════

class BaseDatos:
    """Clase singleton para manejar la conexión a MySQL"""
    
    _instancia = None
    
    def __new__(cls):
        if cls._instancia is None:
            cls._instancia = super().__new__(cls)
            cls._instancia._inicializar()
        return cls._instancia
    
    def _inicializar(self):
        self.config = {
            'host': 'localhost',
            'user': 'root',
            'password': '',  # XAMPP default: sin contraseña
            'database': 'monitor_vitals',
            'charset': 'utf8mb4',
            'collation': 'utf8mb4_spanish_ci'
        }
    
    def obtener_conexion(self):
        """Retorna una nueva conexión a la BD"""
        try:
            conexion = mysql.connector.connect(**self.config)
            return conexion
        except Error as e:
            print(f"[BD Error] {e}")
            return None
    
    def ejecutar_query(self, query, parametros=None, retornar_id=False):
        """Ejecuta un query y retorna resultado si es SELECT"""
        conexion = self.obtener_conexion()
        if not conexion:
            return None
        
        try:
            cursor = conexion.cursor(dictionary=True)
            cursor.execute(query, parametros or ())
            
            if query.strip().upper().startswith('SELECT'):
                resultados = cursor.fetchall()
                cursor.close()
                conexion.close()
                return resultados
            else:
                conexion.commit()
                if retornar_id:
                    id_generado = cursor.lastrowid
                    cursor.close()
                    conexion.close()
                    return id_generado
                cursor.close()
                conexion.close()
                return True
        except Error as e:
            print(f"[BD Error en query] {e}")
            if conexion:
                conexion.rollback()
                conexion.close()
            return None


# ═══════════════════════════════════════════════════════════════
# HILO SERIAL — 3 canales con prefijos P:, T:, E:
# ═══════════════════════════════════════════════════════════════

class HiloSerial(QtCore.QThread):
    dato_pulso = QtCore.pyqtSignal(float)
    dato_temp  = QtCore.pyqtSignal(float)
    dato_ecg   = QtCore.pyqtSignal(float)
    error_conexion = QtCore.pyqtSignal(str)
    estado_msg     = QtCore.pyqtSignal(str)

    def __init__(self, puerto, baudrate=115200):
        super().__init__()
        self.puerto = puerto
        self.baudrate = baudrate
        self.corriendo = True

    def run(self):
        ser = None
        try:
            try:
                ser = serial.Serial(self.puerto, self.baudrate, timeout=0.1)
            except serial.SerialException:
                self.error_conexion.emit(
                    f"No se pudo abrir {self.puerto}.\nVerifique el puerto COM."
                )
                return

            self.estado_msg.emit(f"Puerto {self.puerto} abierto.")
            confirmado = False

            for _ in range(10):
                if not self.corriendo:
                    break
                linea = ser.readline().decode("ascii", errors="ignore").strip()
                if linea:
                    confirmado = True
                    break
                QtCore.QThread.msleep(100)

            if not confirmado:
                ser.close()
                self.error_conexion.emit(f"{self.puerto} sin datos entrantes.")
                return

            while self.corriendo:
                if ser.in_waiting > 0:
                    linea = ser.readline().decode("ascii", errors="ignore").strip()
                    if linea:
                        self._parsear_linea(linea)
                else:
                    QtCore.QThread.msleep(2)

            if ser and ser.is_open:
                ser.close()
        except Exception as e:
            if ser and ser.is_open:
                ser.close()
            self.error_conexion.emit(f"Error: {str(e)}")

    def _parsear_linea(self, linea):
        if linea.startswith("P:"):
            try:
                self.dato_pulso.emit(float(linea[2:]))
            except ValueError:
                pass
        elif linea.startswith("T:"):
            try:
                self.dato_temp.emit(float(linea[2:]))
            except ValueError:
                pass
        elif linea.startswith("E:"):
            try:
                self.dato_ecg.emit(float(linea[2:]))
            except ValueError:
                pass

    def stop(self):
        self.corriendo = False


# ═══════════════════════════════════════════════════════════════
# FUNCIONES DE PROCESAMIENTO DE SEÑAL
# ═══════════════════════════════════════════════════════════════

def crear_filtro_bandpass(lowcut, highcut, fs=200, order=2):
    nyq = fs / 2.0
    b, a = butter(order, [lowcut / nyq, highcut / nyq], btype="band")
    zi = lfilter_zi(b, a)
    return b, a, zi


def detectar_picos_r(senal, fs, umbral_frac=0.25, refractario_s=0.33):
    n = len(senal)
    if n < int(fs * 0.8):
        return []

    ventana = max(3, int(0.05 * fs))
    kernel = np.ones(ventana) / ventana
    señal_suave = np.convolve(senal, kernel, mode="same")
    señal_c = señal_suave - np.mean(señal_suave)

    amp_rango = np.max(señal_c) - np.min(señal_c)
    if amp_rango < 1e-6:
        return []
    umbral = umbral_frac * amp_rango

    picos = []
    for i in range(1, n - 1):
        if (
            señal_c[i] > señal_c[i - 1]
            and señal_c[i] > señal_c[i + 1]
            and señal_c[i] > umbral
        ):
            picos.append(i)

    refractario = int(refractario_s * fs)
    picos_finales = []
    for p in picos:
        if len(picos_finales) == 0 or (p - picos_finales[-1]) > refractario:
            picos_finales.append(p)

    return picos_finales


def convertir_ntc_a_celsius(lectura_adc, v_ref=5.0, adc_max=1023.0,
                             r0=10000.0, b_const=3950.0, t0=298.15):
    v_adc = lectura_adc * v_ref / adc_max
    if v_adc < 0.01:
        return -999.0
    r_ntc = r0 * (v_ref - v_adc) / v_adc
    if r_ntc <= 0:
        return -999.0
    t_kelvin = 1.0 / (1.0 / t0 + (1.0 / b_const) * np.log(r_ntc / r0))
    return t_kelvin - 273.15


# ═══════════════════════════════════════════════════════════════
# FUNCIÓN DE VALIDACIÓN DE RUT CHILENO
# ═══════════════════════════════════════════════════════════════

def validar_rut_chileno(rut):
    """
    Valida formato de RUT chileno: 12345678-9 o 12345678-K
    Retorna: (es_valido, mensaje_error)
    """
    if not rut or len(rut) == 0:
        return False, "El RUT no puede estar vacío"
    
    # Patrón: 7-8 dígitos + guion + 1 dígito o K
    patron = r'^[0-9]{7,8}-[0-9Kk]$'
    
    if not re.match(patron, rut):
        if '-' not in rut:
            return False, "El RUT debe incluir un guion (-)"
        partes = rut.split('-')
        if len(partes) != 2:
            return False, "Formato incorrecto. Use: 12345678-9"
        
        cuerpo = partes[0]
        dv = partes[1]
        
        if not cuerpo.isdigit():
            return False, "El cuerpo del RUT solo debe contener números"
        if len(cuerpo) < 7 or len(cuerpo) > 8:
            return False, "El RUT debe tener 7-8 dígitos antes del guion"
        if len(dv) != 1:
            return False, "Después del guion va un solo carácter (dígito o K)"
        if dv.upper() != 'K' and not dv.isdigit():
            return False, "Después del guion solo se permite un dígito o K"
        
        return False, "Formato incorrecto. Use: 12345678-9"
    
    # Validar dígito verificador
    try:
        cuerpo = rut.split('-')[0]
        dv_ingresado = rut.split('-')[1].upper()
        
        # Calcular DV esperado
        suma = 0
        multiplicador = 2
        for digito in reversed(cuerpo):
            suma += int(digito) * multiplicador
            multiplicador += 1
            if multiplicador > 7:
                multiplicador = 2
        
        resto = 11 - (suma % 11)
        if resto == 11:
            dv_esperado = '0'
        elif resto == 10:
            dv_esperado = 'K'
        else:
            dv_esperado = str(resto)
        
        if dv_ingresado != dv_esperado:
            return False, f"Dígito verificador incorrecto. Se espera: {dv_esperado}"
        
        return True, ""
    except Exception:
        return False, "Error al validar el RUT"


# ═══════════════════════════════════════════════════════════════
# VENTANA PRINCIPAL
# ═══════════════════════════════════════════════════════════════

class MonitorProfesional(QMainWindow):
    def __init__(self):
        super().__init__()
        uic.loadUi("trabajo final.ui", self)

        # ─── Instancia de Base de Datos ───
        self.bd = BaseDatos()
        self.paciente_actual_id = None
        self.paciente_actual_nombre = None
        self._modo_edicion = False
        self._id_editando = None
        
        # ─── Inyectar gráficas pyqtgraph en los contenedores del .ui ───
        # Tab 1: Monitor Principal (ya existen en tu .ui)
        self.grafica_pulso_widget = pg.PlotWidget()
        layout_pulso = QVBoxLayout(self.grafica_pulso)
        layout_pulso.setContentsMargins(0, 0, 0, 0)
        layout_pulso.addWidget(self.grafica_pulso_widget)

        self.grafica_temp_widget = pg.PlotWidget()
        layout_temp = QVBoxLayout(self.grafica_temp)
        layout_temp.setContentsMargins(0, 0, 0, 0)
        layout_temp.addWidget(self.grafica_temp_widget)

        self.grafica_ecg_widget = pg.PlotWidget()
        layout_ecg = QVBoxLayout(self.grafica_ecg)
        layout_ecg.setContentsMargins(0, 0, 0, 0)
        layout_ecg.addWidget(self.grafica_ecg_widget)

        # ─── Tab 4: Inyectar gráfica en widget_grafico ───
        self.grafica_historial_widget = pg.PlotWidget()
        layout_hist = QVBoxLayout(self.widget_grafico)
        layout_hist.setContentsMargins(0, 0, 0, 0)
        layout_hist.addWidget(self.grafica_historial_widget)
        self.curva_historial = None

        # ─── Parámetros globales ───
        self.fs = 200
        self.max_muestras = self.fs * 4
        self.AMPLITUD_OBJETIVO = 60.0

        # ══════════════════════════════════════════
        # CANAL 1: PULSO CARDIACO (A0)
        # ══════════════════════════════════════════
        self.datos_pulso_crudos = deque(maxlen=self.max_muestras)
        self.datos_pulso_filtrados = deque(maxlen=self.max_muestras)
        self.b_pulso, self.a_pulso, self.zi_base_pulso = crear_filtro_bandpass(
            0.5, 5.0, self.fs, 2
        )
        self.zi_pulso = None
        self.ganancia_pulso = 1.0

        self.bpm_pulso_anterior = None
        self.bpm_pulso_valor = 0
        self.historial_bpm_pulso = deque(maxlen=15)
        self.contador_bpm_pulso = 0
        self.alerta_bpm_pulso = False

        self.contador_perdida_pulso = 0
        self.UMBRAL_PERDIDA = 25
        self.señal_perdida_pulso = False
        self.contador_sin_dedo = 0
        self.UMBRAL_SIN_DEDO = 50
        self.dedo_removido = False
        self.hay_picos_pulso = False

        # ══════════════════════════════════════════
        # CANAL 2: TEMPERATURA NTC 10k (A1)
        # ══════════════════════════════════════════
        self.datos_temp_crudos = deque(maxlen=self.max_muestras)
        self.datos_temp_celsius = deque(maxlen=self.max_muestras)
        self.temp_actual = 0.0
        self.temp_mostrada = 0.0  
        self.temp_conectado = False

        # ══════════════════════════════════════════
        # CANAL 3: ECG (A3)
        # ══════════════════════════════════════════
        self.datos_ecg_crudos = deque(maxlen=self.max_muestras)
        self.datos_ecg_filtrados = deque(maxlen=self.max_muestras)
        self.b_ecg, self.a_ecg, self.zi_base_ecg = crear_filtro_bandpass(
            0.5, 40.0, self.fs, 2
        )
        self.zi_ecg = None
        self.ganancia_ecg = 1.0

        self.bpm_ecg_anterior = None
        self.bpm_ecg_valor = 0
        self.historial_bpm_ecg = deque(maxlen=15)
        self.contador_bpm_ecg = 0
        self.alerta_bpm_ecg = False
        self.hay_picos_ecg = False

        self.contador_perdida_ecg = 0
        self.señal_perdida_ecg = False

        # ─── Buffer textEdit ───
        self.contador_text_edit = 0
        self.buffer_text_edit = []

        # ─── Estado general ───
        self.conectado_ok = False
        self.hilo_serial = None

        # ─── Configurar gráficas ───
        self._setup_grafica_pulso()
        self._setup_grafica_temp()
        self._setup_grafica_ecg()
        self._setup_grafica_historial()

        # ─── Timers parpadeo alarmas ───
        self.timer_alarma_pulso = QtCore.QTimer()
        self.timer_alarma_pulso.timeout.connect(self._parpadear_alarma_pulso)
        self.alarma_parpadeo_pulso = False

        self.timer_alarma_ecg = QtCore.QTimer()
        self.timer_alarma_ecg.timeout.connect(self._parpadear_alarma_ecg)
        self.alarma_parpadeo_ecg = False

        # ─── Timer actualización GUI (30 fps) ───
        self.timer_gui = QtCore.QTimer()
        self.timer_gui.timeout.connect(self.actualizar_pantalla)

        # ─── Timer parámetros cada 10 segundos ───
        self.timer_10s = QtCore.QTimer()
        self.timer_10s.timeout.connect(self.calcular_parametros_10s)

        # ═══════════════════════════════════════════════════════
        # CONEXIONES DE BOTONES - TAB 1: MONITOR PRINCIPAL
        # ═══════════════════════════════════════════════════════
        self.boton_conectar.clicked.connect(self.conectar)
        self.boton_desconectar.clicked.connect(self.desconectar)
        self.boton_recibir.clicked.connect(self.alternar_monitor)
        self.boton_recargar_com.clicked.connect(self.listar_puertos)
        self.ingreso_senal.clicked.connect(self.seleccionar_paciente_monitor)

        # ═══════════════════════════════════════════════════════
        # CONEXIONES DE BOTONES - TAB 2: BÚSQUEDA
        # ═══════════════════════════════════════════════════════
        self.boton_buscar.clicked.connect(self.buscar_registros)
        self.boton_limpiar.clicked.connect(self.limpiar_busqueda)
        self.boton_editar.clicked.connect(self.editar_registro_seleccionado)
        self.boton_editar_paciente.clicked.connect(self.ir_a_editar_paciente_desde_busqueda)
        # ═══════════════════════════════════════════════════════
        # CONEXIONES DE BOTONES - TAB 3: DATOS DEMOGRÁFICOS
        # ═══════════════════════════════════════════════════════
        self.pushButton_guardarpac.clicked.connect(self.guardar_paciente)
        self.pushButton_2_limpiarpac.clicked.connect(self.limpiar_formulario_paciente)
        
        # Validación RUT en tiempo real
        self.rut_edit = self.findChild(QtWidgets.QLineEdit, "rut_edit")
        if self.rut_edit:
            self.rut_edit.textChanged.connect(self.validar_rut_tiempo_real)

        # ═══════════════════════════════════════════════════════
        # CONEXIONES DE BOTONES - TAB 4: HISTORIAL GRÁFICO
        # ═══════════════════════════════════════════════════════
        self.pushButton_3.clicked.connect(self.graficar_historial)
        self.pushButton_2.clicked.connect(self.limpiar_historial)

        # ─── Configurar Tabla de Búsqueda ───
        self._configurar_tabla_busqueda()

        # ─── Configurar formulario paciente ───
        self._configurar_formulario_paciente()

        # ─── Iniciar ───
                # ─── Forzar formato correcto en DateTimeEdit ───
        formato_fecha = "dd/MM/yyyy HH:mm:ss"
        
        # Tab 2: Búsqueda
        self.dateTimeEdit.setDisplayFormat(formato_fecha)
        self.dateTimeEdit_2.setDisplayFormat(formato_fecha)
        
        # Tab 4: Historial Gráfico
        self.dateTimeEdit_3.setDisplayFormat(formato_fecha)
        self.dateTimeEdit_4.setDisplayFormat(formato_fecha)
        
        # Establecer fechas por defecto (últimas 24 horas) para que no arranquen en 2000
        ahora = QtCore.QDateTime.currentDateTime()
        ayer = ahora.addDays(-1)
        self.dateTimeEdit.setDateTime(ayer)
        self.dateTimeEdit_2.setDateTime(ahora)
        self.dateTimeEdit_3.setDateTime(ayer)
        self.dateTimeEdit_4.setDateTime(ahora)
        self.listar_puertos()
        self._set_estado_pulso("DESCONECTADO", "gray")
        self._set_estado_temp("DESCONECTADO", "gray")
        self._set_estado_ecg("DESCONECTADO", "gray")

        # Mostrar ID vacío al inicio
        self.ID_edit.setText("")

    # ═══════════════════════════════════════════════════════════
    # SETUP DE GRÁFICAS
    # ═══════════════════════════════════════════════════════════

    def _setup_grafica_pulso(self):
        g = self.grafica_pulso_widget
        g.setBackground("w")
        g.disableAutoRange(axis="y")
        g.showGrid(x=False, y=True, alpha=0.3)
        g.setLabel("left", "Amplitud")
        g.setLabel("bottom", "Muestras")
        self.curva_pulso = g.plot(
            pen=pg.mkPen(color=(41, 128, 185), width=2), antialias=True
        )
        self.scatter_pulso = pg.ScatterPlotItem(
            pen=pg.mkPen(None), brush=pg.mkBrush(255, 0, 0, 200),
            size=12, symbol="o"
        )
        g.addItem(self.scatter_pulso)
        self.textos_peaks_pulso = []

    def _setup_grafica_temp(self):
        g = self.grafica_temp_widget
        g.setBackground("w")
        g.disableAutoRange(axis="y")
        g.showGrid(x=False, y=True, alpha=0.3)
        g.setLabel("left", "Temperatura (°C)")
        g.setLabel("bottom", "Muestras")
        self.curva_temp = g.plot(
            pen=pg.mkPen(color=(39, 174, 96), width=2), antialias=True
        )
        g.setYRange(20, 45, padding=0)

    def _setup_grafica_ecg(self):
        g = self.grafica_ecg_widget
        g.setBackground("w")
        g.disableAutoRange(axis="y")
        g.showGrid(x=False, y=True, alpha=0.3)
        g.setLabel("left", "Amplitud")
        g.setLabel("bottom", "Muestras")
        self.curva_ecg = g.plot(
            pen=pg.mkPen(color=(142, 68, 173), width=2), antialias=True
        )
        self.scatter_ecg = pg.ScatterPlotItem(
            pen=pg.mkPen(None), brush=pg.mkBrush(255, 0, 0, 200),
            size=12, symbol="o"
        )
        g.addItem(self.scatter_ecg)
        self.textos_peaks_ecg = []

    def _setup_grafica_historial(self):
        """Configurar la gráfica del historial (Tab 4)"""
        g = self.grafica_historial_widget
        g.setBackground("w")
        g.showGrid(x=True, y=True, alpha=0.3)
        g.setLabel("left", "Valor")
        g.setLabel("bottom", "Fecha/Hora")
        g.setTitle("Historial de Señales")
        self.curva_historial = g.plot(
            pen=pg.mkPen(color=(41, 128, 185), width=2), 
            symbol='o', symbolSize=5, antialias=True
        )

    # ═══════════════════════════════════════════════════════════
    # CONFIGURACIÓN TABLA DE BÚSQUEDA (TAB 2)
    # ═══════════════════════════════════════════════════════════

    def _configurar_tabla_busqueda(self):
        """Configura las columnas del tableWidget"""
        self.tableWidget.setColumnCount(5)
        self.tableWidget.setHorizontalHeaderLabels([
            "Fecha/Hora", 
            "Temp. Promedio (°C)", 
            "FC ECG (BPM)", 
            "FC Pulso (BPM)",
            "ID Registro"
        ])
        self.tableWidget.horizontalHeader().setStretchLastSection(True)
        self.tableWidget.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tableWidget.setEditTriggers(QAbstractItemView.NoEditTriggers)
        
        # Ajustar ancho de columnas
        self.tableWidget.setColumnWidth(0, 180)
        self.tableWidget.setColumnWidth(1, 140)
        self.tableWidget.setColumnWidth(2, 120)
        self.tableWidget.setColumnWidth(3, 120)

    # ═══════════════════════════════════════════════════════════
    # CONFIGURACIÓN FORMULARIO PACIENTE (TAB 3)
    # ═══════════════════════════════════════════════════════════

    def _configurar_formulario_paciente(self):
        """Configura el formulario de datos demográficos"""
        # ID: Bloqueado (solo lectura, autogenerado)
        self.ID_edit.setReadOnly(True)
        self.ID_edit.setStyleSheet(
            "QLineEdit { "
            "background-color: #e0e0e0; "
            "color: #666666; "
            "border: 1px solid #cccccc; "
            "font-weight: bold; "
            "}"
        )
        self.ID_edit.setPlaceholderText("Autogenerado")

        # Configurar rango del spinBox edad
        self.spinBox_edad.setRange(0, 150)
        self.spinBox_edad.setValue(0)

        # Configurar rango del doubleSpinBox peso
        self.peso_edit.setRange(0.01, 500.00)
        self.peso_edit.setDecimals(2)
        self.peso_edit.setSuffix(" kg")

        # Configurar rango del doubleSpinBox altura
        self.altura_edit.setRange(0.01, 300.00)
        self.altura_edit.setDecimals(2)
        self.altura_edit.setSuffix(" cm")

        # Configurar comboBox sexo
        self.comboBox_sexo.clear()
        self.comboBox_sexo.addItems(["Masculino", "Femenino"])

    # ═══════════════════════════════════════════════════════════
    # VALIDACIÓN DE RUT EN TIEMPO REAL (TAB 3)
    # ═══════════════════════════════════════════════════════════

    def validar_rut_tiempo_real(self, texto):
        """Valida el RUT mientras el usuario escribe"""
        if not texto:
            self.rut_edit.setStyleSheet("")
            return
        
        # Verificar formato básico mientras escribe
        es_valido, _ = validar_rut_chileno(texto)
        
        if es_valido:
            self.rut_edit.setStyleSheet(
                "QLineEdit { "
                "background-color: #d4edda; "
                "border: 2px solid #28a745; "
                "color: #155724; "
                "font-weight: bold; "
                "}"
            )
        else:
            # Solo mostrar rojo si ya escribió el guion (terminó de escribir)
            if '-' in texto:
                self.rut_edit.setStyleSheet(
                    "QLineEdit { "
                    "background-color: #f8d7da; "
                    "border: 2px solid #dc3545; "
                    "color: #721c24; "
                    "}"
                )
            else:
                self.rut_edit.setStyleSheet("")

    # ═══════════════════════════════════════════════════════════
    # TAB 3: GUARDAR PACIENTE
    # ═══════════════════════════════════════════════════════════

    def guardar_paciente(self):
        """Guarda uno nuevo o actualiza si viene de Tab 2"""
        
        # ══════════════════════════════════════════
        # CASO 1: MODO ACTUALIZACIÓN (Viene de Tab 2)
        # ══════════════════════════════════════════
        if self._modo_edicion:
            nombre = self.nombre_edit.text().strip()
            apellido = self.apellido_edit.text().strip()
            edad = self.spinBox_edad.value()
            sexo = self.comboBox_sexo.currentText()
            peso = self.peso_edit.value()
            altura = self.altura_edit.value()
            obs = self.textEdit_obs.toPlainText().strip()
            
            if not nombre:
                QMessageBox.warning(self, "Campo Requerido", "Debe ingresar el nombre.")
                self.nombre_edit.setFocus()
                return
            if not apellido:
                QMessageBox.warning(self, "Campo Requerido", "Debe ingresar el apellido.")
                self.apellido_edit.setFocus()
                return
            
            exito = self.bd.ejecutar_query(
                """UPDATE pacientes SET nombre=%s, apellido=%s, edad=%s, sexo=%s, 
                   peso=%s, altura=%s, observaciones=%s WHERE id_paciente=%s""",
                (nombre, apellido, edad, sexo, peso, altura, obs, self._id_editando)
            )
            
            if exito:
                QMessageBox.information(self, "Actualizado", 
                    f"Paciente actualizado correctamente.\nID: {self._id_editando}")
                self._log(f"[BD] Paciente ID {self._id_editando} actualizado.")
                self.limpiar_formulario_paciente() # Esto ya resetea el modo edición
            else:
                QMessageBox.critical(self, "Error", "Error al actualizar en la BD.")
            return

        # ══════════════════════════════════════════
        # CASO 2: MODO NUEVO (Flujo normal)
        # ══════════════════════════════════════════
        rut = self.rut_edit.text().strip() if self.rut_edit else ""
        nombre = self.nombre_edit.text().strip()
        apellido = self.apellido_edit.text().strip()
        edad = self.spinBox_edad.value()
        sexo = self.comboBox_sexo.currentText()
        peso = self.peso_edit.value()
        altura = self.altura_edit.value()
        observaciones = self.textEdit_obs.toPlainText().strip()

        es_valido, msg_error = validar_rut_chileno(rut)
        if not es_valido:
            QMessageBox.warning(self, "RUT Inválido", f"{msg_error}\n\nFormato: 12345678-9")
            self.rut_edit.setFocus()
            self.rut_edit.selectAll()
            return

        if not nombre:
            QMessageBox.warning(self, "Campo Requerido", "Debe ingresar el nombre del paciente.")
            self.nombre_edit.setFocus()
            return

        if not apellido:
            QMessageBox.warning(self, "Campo Requerido", "Debe ingresar el apellido del paciente.")
            self.apellido_edit.setFocus()
            return

        resultado = self.bd.ejecutar_query(
            "SELECT id_paciente, nombre, apellido FROM pacientes WHERE rut = %s", (rut,)
        )
        if resultado and len(resultado) > 0:
            pac = resultado[0]
            respuesta = QMessageBox.question(
                self, "RUT Duplicado",
                f"⚠️ Ya existe un paciente con RUT {rut}:\n• {pac['nombre']} {pac['apellido']}\n\n¿Desea actualizar sus datos?",
                QMessageBox.Yes | QMessageBox.No
            )
            if respuesta == QMessageBox.Yes:
                exito = self.bd.ejecutar_query(
                    """UPDATE pacientes SET nombre=%s, apellido=%s, edad=%s, sexo=%s, 
                       peso=%s, altura=%s, observaciones=%s WHERE rut=%s""",
                    (nombre, apellido, edad, sexo, peso, altura, observaciones, rut)
                )
                if exito:
                    QMessageBox.information(self, "Actualizado", f"Paciente actualizado.\nID: {pac['id_paciente']}")
                    self.ID_edit.setText(str(pac['id_paciente']))
                else:
                    QMessageBox.critical(self, "Error", "Error al actualizar.")
            return

        id_nuevo = self.bd.ejecutar_query(
            """INSERT INTO pacientes (rut, nombre, apellido, edad, sexo, peso, altura, observaciones) 
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (rut, nombre, apellido, edad, sexo, peso, altura, observaciones),
            retornar_id=True
        )

        if id_nuevo:
            self.ID_edit.setText(str(id_nuevo))
            QMessageBox.information(self, "Paciente Guardado", 
                f"Paciente registrado.\n\n• ID: {id_nuevo}\n• RUT: {rut}\n• Nombre: {nombre} {apellido}")
            self._log(f"[BD] Paciente guardado - ID: {id_nuevo}, RUT: {rut}")
        else:
            QMessageBox.critical(self, "Error", "No se pudo guardar el paciente.")

    # ═══════════════════════════════════════════════════════════
    # TAB 3: LIMPIAR FORMULARIO PACIENTE
    # ═══════════════════════════════════════════════════════════

    def limpiar_formulario_paciente(self):
        """Limpia todos los campos y sale del modo edición"""
        
        # ★ Resetear modo edición
        self._modo_edicion = False
        self._id_editando = None
        
        # Restaurar botón guardar
        self.pushButton_guardarpac.setText("Guardar Paciente")
        self.pushButton_guardarpac.setStyleSheet("")
        
        # Desbloquear RUT y quitar estilos
        if self.rut_edit:
            self.rut_edit.setReadOnly(False)
            self.rut_edit.setText("")
            self.rut_edit.setStyleSheet("")
        
        # Limpiar campos
        self.ID_edit.setText("")
        self.nombre_edit.setText("")
        self.nombre_edit.setStyleSheet("") # Quitar borde amarillo
        self.apellido_edit.setText("")
        self.apellido_edit.setStyleSheet("") # Quitar borde amarillo
        self.spinBox_edad.setValue(0)
        self.comboBox_sexo.setCurrentIndex(0)
        self.peso_edit.setValue(0.01)
        self.altura_edit.setValue(0.01)
        self.textEdit_obs.clear()

    # ═══════════════════════════════════════════════════════════
    # TAB 1: SELECCIONAR PACIENTE PARA MONITOREO
    # ═══════════════════════════════════════════════════════════

    def seleccionar_paciente_monitor(self):
        """Busca paciente por ID o RUT para iniciar monitoreo"""
        texto = self.paciente_registrar.text().strip()
        
        if not texto:
            QMessageBox.warning(
                self, 
                "Paciente Requerido", 
                "❌ Debe ingresar el ID o RUT del paciente\n"
                "para poder registrar las señales."
            )
            self.paciente_registrar.setFocus()
            return
        
        # Buscar por ID o RUT
        query = """
            SELECT id_paciente, rut, nombre, apellido 
            FROM pacientes 
            WHERE id_paciente = %s OR rut = %s
        """
        resultado = self.bd.ejecutar_query(query, (texto, texto))
        
        if not resultado or len(resultado) == 0:
            QMessageBox.warning(
                self, 
                "Paciente No Encontrado", 
                f"❌ No existe ningún paciente con ID o RUT: {texto}\n\n"
                f"Primero debe registrar el paciente en la pestaña 'Datos Demográficos'."
            )
            self.paciente_registrar.setFocus()
            self.paciente_registrar.selectAll()
            return
        
        paciente = resultado[0]
        nombre_completo = f"{paciente['nombre']} {paciente['apellido']}"
        
        # Confirmar selección
        respuesta = QMessageBox.question(
            self,
            "Confirmar Paciente",
            f"¿Está seguro que quiere ingresar señales al paciente:\n\n"
            f"• ID: {paciente['id_paciente']}\n"
            f"• RUT: {paciente['rut']}\n"
            f"• Nombre: {nombre_completo}\n\n"
            f"Las señales se guardarán asociadas a este paciente.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if respuesta == QMessageBox.Yes:
            self.paciente_actual_id = paciente['id_paciente']
            self.paciente_actual_nombre = nombre_completo
            self.paciente_registrar.setStyleSheet(
                "QLineEdit { "
                "background-color: #d4edda; "
                "border: 2px solid #28a745; "
                "color: #155724; "
                "font-weight: bold; "
                "}"
            )
            self._log(f"[BD] Paciente seleccionado: {nombre_completo} (ID: {self.paciente_actual_id})")
            QMessageBox.information(
                self, 
                "Paciente Seleccionado", 
                f"✅ Paciente seleccionado:\n{nombre_completo}\n\n"
                f"Ahora puede iniciar la recepción de datos."
            )

    # ═══════════════════════════════════════════════════════════
    # TAB 2: BUSCAR REGISTROS
    # ═══════════════════════════════════════════════════════════

    def buscar_registros(self):
        """Busca registros en la BD según filtros"""
        
        # Obtener filtros
        id_rut = self.id_1.text().strip()
        fecha_inicio = self.dateTimeEdit.dateTime().toString("yyyy-MM-dd hh:mm:ss")
        fecha_fin = self.dateTimeEdit_2.dateTime().toString("yyyy-MM-dd hh:mm:ss")
        param = self.comboBox_param.currentText()
        
        # Construir query dinámico
        condiciones = []
        parametros = []
        
        if id_rut:
            condiciones.append("(p.id_paciente = %s OR p.rut = %s)")
            parametros.extend([id_rut, id_rut])
        
        condiciones.append("r.fecha_hora BETWEEN %s AND %s")
        parametros.extend([fecha_inicio, fecha_fin])
        
        if param == "Todos":
            select = """r.fecha_hora, r.temperatura_promedio, 
                       r.fc_ecg, r.fc_pulso, r.id_registro, r.id_paciente"""
        elif param == "Temperatura":
            select = """r.fecha_hora, r.temperatura_promedio, 
                       NULL as fc_ecg, NULL as fc_pulso, r.id_registro, r.id_paciente"""
            condiciones.append("r.temperatura_promedio IS NOT NULL")
        elif param == "Frecuencia cardíaca ECG":
            select = """r.fecha_hora, NULL as temperatura_promedio, 
                       r.fc_ecg, NULL as fc_pulso, r.id_registro, r.id_paciente"""
            condiciones.append("r.fc_ecg IS NOT NULL")
        elif param == "Frecuencia cardíaca Pulso":
            select = """r.fecha_hora, NULL as temperatura_promedio, 
                       NULL as fc_ecg, r.fc_pulso, r.id_registro, r.id_paciente"""
            condiciones.append("r.fc_pulso IS NOT NULL")
        
        where_clause = " AND ".join(condiciones)
        
        query = f"""
            SELECT {select}
            FROM registros_vitales r
            INNER JOIN pacientes p ON r.id_paciente = p.id_paciente
            WHERE {where_clause}
            ORDER BY r.fecha_hora ASC
        """
        
        resultados = self.bd.ejecutar_query(query, parametros)
        
        self.tableWidget.setRowCount(0)
        self.tableWidget.setColumnCount(6) # ★ Ahora son 6 columnas
        
        if not resultados or len(resultados) == 0:
            QMessageBox.information(self, "Sin Resultados", "📋 No se encontraron registros.")
            return
        
        self.tableWidget.setRowCount(len(resultados))
        for fila, reg in enumerate(resultados):
            fh = reg['fecha_hora']
            fh_str = fh.strftime("%d/%m/%Y %H:%M:%S") if isinstance(fh, datetime) else str(fh)
            self.tableWidget.setItem(fila, 0, QtWidgets.QTableWidgetItem(fh_str))
            
            temp = reg['temperatura_promedio']
            self.tableWidget.setItem(fila, 1, QtWidgets.QTableWidgetItem(f"{temp:.2f}" if temp else "-"))
            
            fc_ecg = reg['fc_ecg']
            self.tableWidget.setItem(fila, 2, QtWidgets.QTableWidgetItem(str(fc_ecg) if fc_ecg else "-"))
            
            fc_pulso = reg['fc_pulso']
            self.tableWidget.setItem(fila, 3, QtWidgets.QTableWidgetItem(str(fc_pulso) if fc_pulso else "-"))
            
            # ID Registro (gris)
            item_id = QtWidgets.QTableWidgetItem(str(reg['id_registro']))
            item_id.setForeground(QtGui.QColor(200, 200, 200))
            self.tableWidget.setItem(fila, 4, item_id)
            
            # ★ ID Paciente (invisible, para usarlo al editar)
            item_pac = QtWidgets.QTableWidgetItem(str(reg['id_paciente']))
            item_pac.setForeground(QtGui.QColor(240, 240, 240)) # Casi invisible
            self.tableWidget.setItem(fila, 5, item_pac)
            
        self._log(f"[BD] Búsqueda: {len(resultados)} registros encontrados")

    # ═══════════════════════════════════════════════════════════
    # TAB 2: LIMPIAR BÚSQUEDA
    # ═══════════════════════════════════════════════════════════

    def limpiar_busqueda(self):
        """Limpia los filtros y la tabla de búsqueda"""
        self.id_1.setText("")
        
        
        self.comboBox_param.setCurrentIndex(0)
        self.tableWidget.setRowCount(0)

    # ═══════════════════════════════════════════════════════════
    # TAB 2: EDITAR REGISTRO SELECCIONADO
    # ═══════════════════════════════════════════════════════════
    def ir_a_editar_paciente_desde_busqueda(self):
        """Toma el ID del paciente de la fila seleccionada, llena el formulario y SALTA a Tab 3"""
        
        fila = self.tableWidget.currentRow()
        if fila < 0:
            QMessageBox.warning(self, "Sin Selección", 
                "Seleccione un registro de la tabla para editar su paciente.")
            return
        
        # Obtener el ID del paciente de la columna 5 (la invisible)
        item_pac = self.tableWidget.item(fila, 5)
        if not item_pac:
            return
        
        id_paciente = item_pac.text()
        
        # Buscar datos completos del paciente
        pac = self.bd.ejecutar_query(
            """SELECT id_paciente, rut, nombre, apellido, edad, sexo, 
                      peso, altura, observaciones 
               FROM pacientes WHERE id_paciente = %s""", (id_paciente,)
        )
        
        if not pac or len(pac) == 0:
            QMessageBox.critical(self, "Error", "Paciente no encontrado en la BD.")
            return
        
        p = pac[0]
        
        # ─── Llenar formulario Tab 3 (SIN bloquear el RUT) ───
        self.ID_edit.setText(str(p['id_paciente']))
        
        if self.rut_edit:
            self.rut_edit.setText(p['rut'])
            # Ya NO se bloquea, queda libre para editar si te equivocaste
            self.rut_edit.setStyleSheet(
                "QLineEdit { border: 2px solid #ffc107; background-color: #fff9e6; }"
            )
        
        self.nombre_edit.setText(p['nombre'] or "")
        self.apellido_edit.setText(p['apellido'] or "")
        self.spinBox_edad.setValue(p['edad'] if p['edad'] else 0)
        
        if p['sexo'] == "Masculino": self.comboBox_sexo.setCurrentIndex(0)
        elif p['sexo'] == "Femenino": self.comboBox_sexo.setCurrentIndex(1)
        
        self.peso_edit.setValue(float(p['peso']) if p['peso'] else 0.01)
        self.altura_edit.setValue(float(p['altura']) if p['altura'] else 0.01)
        self.textEdit_obs.setPlainText(p['observaciones'] or "")
        
        # Resaltar los demás campos editables
        self.nombre_edit.setStyleSheet("QLineEdit { border: 2px solid #ffc107; background-color: #fff9e6; }")
        self.apellido_edit.setStyleSheet("QLineEdit { border: 2px solid #ffc107; background-color: #fff9e6; }")
        
        # ─── Activar Modo Edición ───
        self._modo_edicion = True
        self._id_editando = p['id_paciente']
        
        # Cambiar texto del botón
        self.pushButton_guardarpac.setText("Actualizar paciente")
        self.pushButton_guardarpac.setStyleSheet(
            "QPushButton { background-color: #ffc107; color: black; font-weight: bold; padding: 8px; border-radius: 4px; }"
            "QPushButton:hover { background-color: #e0a800; }"
        )
        
        # ─── REDIRECCIÓN AUTOMÁTICA A TAB 3 ───
        # Usamos findChild por si en tu .ui el tabWidget tiene otro nombre
        tabs = self.findChild(QtWidgets.QTabWidget)
        if tabs:
            tabs.setCurrentIndex(2)  # El índice 2 es la 3ra pestaña (Datos Demográficos)
        
        # Poner el foco en el nombre para que empieces a escribir de inmediato
        self.nombre_edit.setFocus()
        self.nombre_edit.selectAll()
        
        self._log(f"[BD] Modo edición activado para paciente ID: {p['id_piente']}. Redirigido a formulario.")


    def editar_registro_seleccionado(self):
        """Permite editar la fecha/hora del registro seleccionado"""
        
        fila = self.tableWidget.currentRow()
        if fila < 0:
            QMessageBox.warning(self, "Sin Selección", 
                "Seleccione un registro de la tabla para editar.")
            return
        
        # Obtener ID del registro
        item_id = self.tableWidget.item(fila, 4)
        if not item_id:
            return
        
        id_registro = item_id.text()
        
        # Obtener datos actuales
        resultado = self.bd.ejecutar_query(
            "SELECT * FROM registros_vitales WHERE id_registro = %s",
            (id_registro,)
        )
        
        if not resultado or len(resultado) == 0:
            QMessageBox.critical(self, "Error", "Registro no encontrado en la BD.")
            return
        
        reg = resultado[0]
        
        # Crear diálogo de edición
        dialogo = QtWidgets.QDialog(self)
        dialogo.setWindowTitle("Editar Registro")
        dialogo.setFixedSize(350, 250)
        layout = QtWidgets.QFormLayout(dialogo)
        
        # Campo fecha/hora
        fecha_actual = reg['fecha_hora']
        if isinstance(fecha_actual, datetime):
            qdt = QtCore.QDateTime(
                QDate(fecha_actual.year, fecha_actual.month, fecha_actual.day),
                QTime(fecha_actual.hour, fecha_actual.minute, fecha_actual.second)
            )
        else:
            qdt = QtCore.QDateTime.currentDateTime()
        
        datetime_edit = QtWidgets.QDateTimeEdit(qdt)
        datetime_edit.setCalendarPopup(True)
        datetime_edit.setDisplayFormat("dd/MM/yyyy hh:mm:ss")
        layout.addRow("Fecha/Hora:", datetime_edit)
        
        # Campos de valores (solo lectura para referencia)
        temp = reg['temperatura_promedio']
        fc_ecg = reg['fc_ecg']
        fc_pulso = reg['fc_pulso']
        
        layout.addRow("Temp (°C):", QtWidgets.QLabel(f"{temp:.2f}" if temp else "-"))
        layout.addRow("FC ECG:", QtWidgets.QLabel(str(fc_ecg) if fc_ecg else "-"))
        layout.addRow("FC Pulso:", QtWidgets.QLabel(str(fc_pulso) if fc_pulso else "-"))
        
        # Botones
        btn_box = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        btn_box.accepted.connect(dialogo.accept)
        btn_box.rejected.connect(dialogo.reject)
        layout.addRow(btn_box)
        
        if dialogo.exec_() == QtWidgets.QDialog.Accepted:
            nueva_fecha = datetime_edit.dateTime().toString("yyyy-MM-dd hh:mm:ss")
            
            exito = self.bd.ejecutar_query(
                "UPDATE registros_vitales SET fecha_hora = %s WHERE id_registro = %s",
                (nueva_fecha, id_registro)
            )
            
            if exito:
                QMessageBox.information(self, "Actualizado", 
                    "Fecha/hora actualizada correctamente.")
                self.buscar_registros()  # Refrescar tabla
            else:
                QMessageBox.critical(self, "Error", "Error al actualizar el registro.")

    # ═══════════════════════════════════════════════════════════
    # TAB 4: GRAFICAR HISTORIAL
    # ═══════════════════════════════════════════════════════════

    def graficar_historial(self):
        """Grafica los datos históricos de la BD"""
        
        id_rut = self.id_2.text().strip()
        fecha_inicio = self.dateTimeEdit_3.dateTime().toString("yyyy-MM-dd hh:mm:ss")
        fecha_fin = self.dateTimeEdit_4.dateTime().toString("yyyy-MM-dd hh:mm:ss")
        tipo_senal = self.comboBox_senal.currentText()
        
        # Validar ID/RUT
        if not id_rut:
            QMessageBox.warning(self, "Campo Requerido", 
                "❌ Ingrese el ID o RUT del paciente.")
            self.id_2.setFocus()
            return
        
        # Verificar que el paciente existe
        pac = self.bd.ejecutar_query(
            "SELECT id_paciente, nombre, apellido FROM pacientes WHERE id_paciente = %s OR rut = %s",
            (id_rut, id_rut)
        )
        if not pac or len(pac) == 0:
            QMessageBox.warning(self, "Paciente No Encontrado", 
                f"❌ No existe paciente con ID/RUT: {id_rut}")
            return
        
        id_paciente = pac[0]['id_paciente']
        nombre_pac = f"{pac[0]['nombre']} {pac[0]['apellido']}"
        
        # Determinar qué campo consultar según tipo de señal
        if tipo_senal == "ECG":
            campo = "fc_ecg"
            titulo = f"FC ECG - {nombre_pac}"
            color = (142, 68, 173)  # Morado
            unidad = "BPM"
        elif tipo_senal == "Pulso":
            campo = "fc_pulso"
            titulo = f"FC Pulso - {nombre_pac}"
            color = (41, 128, 185)  # Azul
            unidad = "BPM"
        elif tipo_senal == "Temperatura":
            campo = "temperatura_promedio"
            titulo = f"Temperatura - {nombre_pac}"
            color = (39, 174, 96)  # Verde
            unidad = "°C"
        else:
            return
        
        # Consultar datos
        query = f"""
            SELECT fecha_hora, {campo} as valor
            FROM registros_vitales
            WHERE id_paciente = %s 
              AND fecha_hora BETWEEN %s AND %s
              AND {campo} IS NOT NULL
            ORDER BY fecha_hora ASC
        """
        resultados = self.bd.ejecutar_query(query, (id_paciente, fecha_inicio, fecha_fin))
        
        if not resultados or len(resultados) == 0:
            QMessageBox.information(self, "Sin Datos", 
                f"📋 No hay registros de {tipo_senal} para este paciente\n"
                f"en el rango de fechas seleccionado.")
            self.grafica_historial_widget.clear()
            return
        
        # Preparar datos para graficar
        fechas = []
        valores = []
        
        for reg in resultados:
            fh = reg['fecha_hora']
            if isinstance(fh, datetime):
                # Convertir a timestamp para el eje X
                timestamp = fh.timestamp()
                fechas.append(timestamp)
            else:
                fechas.append(len(fechas))
            valores.append(float(reg['valor']))
        
        fechas_np = np.array(fechas)
        valores_np = np.array(valores)
        
        # Limpiar y configurar gráfica
        self.grafica_historial_widget.clear()
        self.grafica_historial_widget.setBackground("w")
        self.grafica_historial_widget.showGrid(x=True, y=True, alpha=0.3)
        self.grafica_historial_widget.setTitle(titulo)
        self.grafica_historial_widget.setLabel("left", tipo_senal, units=unidad)
        self.grafica_historial_widget.setLabel("bottom", "Fecha/Hora")
        
        # Crezar curva
        self.curva_historial = self.grafica_historial_widget.plot(
            x=fechas_np,
            y=valores_np,
            pen=pg.mkPen(color=color, width=2),
            symbol='o',
            symbolSize=6,
            symbolBrush=pg.mkBrush(color),
            antialias=True
        )
        
        # Configurar ejes
        axis = pg.DateAxisItem(orientation='bottom')
        self.grafica_historial_widget.setAxisItems({'bottom': axis})
        
        # Ajustar rango Y
        y_min = np.min(valores_np) - 5
        y_max = np.max(valores_np) + 5
        self.grafica_historial_widget.setYRange(y_min, y_max, padding=0.1)
        
        # Agregar texto con estadísticas
        promedio = np.mean(valores_np)
        maximo = np.max(valores_np)
        minimo = np.min(valores_np)
        
        stats_text = pg.TextItem(
            text=f"Prom: {promedio:.1f} | Max: {maximo:.1f} | Min: {minimo:.1f} {unidad}",
            color=(0, 0, 0),
            anchor=(0.5, 0)
        )
        stats_text.setFont(QtGui.QFont("Arial", 10))
        stats_text.setPos(np.mean(fechas_np), y_max - 2)
        self.grafica_historial_widget.addItem(stats_text)
        
        self._log(f"[BD] Gráfico {tipo_senal}: {len(resultados)} puntos")

    # ═══════════════════════════════════════════════════════════
    # TAB 4: LIMPIAR HISTORIAL
    # ═══════════════════════════════════════════════════════════

    def limpiar_historial(self):
        """Limpia el formulario y gráfica del historial"""
        self.id_2.setText("")
        
        self.comboBox_senal.setCurrentIndex(0)
        
        self.grafica_historial_widget.clear()
        self.grafica_historial_widget.setBackground("w")
        self.grafica_historial_widget.showGrid(x=True, y=True, alpha=0.3)
        self.grafica_historial_widget.setTitle("Historial de Señales")
        self.grafica_historial_widget.setLabel("left", "Valor")
        self.grafica_historial_widget.setLabel("bottom", "Fecha/Hora")

    # ═══════════════════════════════════════════════════════════
    # HELPERS UI
    # ═══════════════════════════════════════════════════════════

    def _log(self, msg):
        self.monitor_serial.append(
            f"[{QtCore.QTime.currentTime().toString('hh:mm:ss')}] {msg}"
        )

    def _set_estado_pulso(self, texto, color):
        self.indicador_alerta_bpm.setText(texto)
        self.indicador_alerta_bpm.setStyleSheet(
            f"background-color:{color};color:white;border-radius:5px;"
            f"font-weight:bold;font-size:12px;padding:5px;"
        )

    def _set_estado_temp(self, texto, color):
        self.indicador_alerta_temp.setText(texto)
        self.indicador_alerta_temp.setStyleSheet(
            f"background-color:{color};color:white;border-radius:5px;"
            f"font-weight:bold;font-size:12px;padding:5px;"
        )

    def _set_estado_ecg(self, texto, color):
        self.indicador_alerta_ecg.setText(texto)
        self.indicador_alerta_ecg.setStyleSheet(
            f"background-color:{color};color:white;border-radius:5px;"
            f"font-weight:bold;font-size:12px;padding:5px;"
        )

    def _parpadear_alarma_pulso(self):
        self.alarma_parpadeo_pulso = not self.alarma_parpadeo_pulso
        c = "#FF0000" if self.alarma_parpadeo_pulso else "#8B0000"
        self.indicador_alerta_bpm.setStyleSheet(
            f"background-color:{c};color:white;border-radius:5px;"
            f"font-weight:bold;font-size:12px;padding:5px;"
        )
        self.label_bpm.setStyleSheet(
            f"color:{c};font-weight:bold;font-size:24px;"
        )

    def _parpadear_alarma_ecg(self):
        self.alarma_parpadeo_ecg = not self.alarma_parpadeo_ecg
        c = "#FF0000" if self.alarma_parpadeo_ecg else "#8B0000"
        self.indicador_alerta_ecg.setStyleSheet(
            f"background-color:{c};color:white;border-radius:5px;"
            f"font-weight:bold;font-size:12px;padding:5px;"
        )
        self.label_ecg.setStyleSheet(
            f"color:{c};font-weight:bold;font-size:24px;"
        )

    def _detener_alarma_pulso(self):
        self.timer_alarma_pulso.stop()
        self.alarma_parpadeo_pulso = False
        self.label_bpm.setStyleSheet("color:black;font-size:24px;")

    def _detener_alarma_ecg(self):
        self.timer_alarma_ecg.stop()
        self.alarma_parpadeo_ecg = False
        self.label_ecg.setStyleSheet("color:black;font-size:24px;")

    # ═══════════════════════════════════════════════════════════
    # PUERTOS SERIALES
    # ═══════════════════════════════════════════════════════════

    def listar_puertos(self):
        self.combo_puertos.clear()
        ports = serial.tools.list_ports.comports()
        for p in ports:
            self.combo_puertos.addItem(p.device)
        if not ports:
            self.combo_puertos.addItem("(sin puertos)")

    # ═══════════════════════════════════════════════════════════
    # CONEXIÓN / DESCONEXIÓN
    # ═══════════════════════════════════════════════════════════

    def conectar(self):
        puerto = self.combo_puertos.currentText()
        if not puerto or puerto == "(sin puertos)" or self.hilo_serial:
            return

        self._resetear_todo()
        self._set_estado_pulso("CONECTANDO...", "#E67E22")
        self._set_estado_temp("CONECTANDO...", "#E67E22")
        self._set_estado_ecg("CONECTANDO...", "#E67E22")

        self.hilo_serial = HiloSerial(puerto)
        self.hilo_serial.dato_pulso.connect(self.recibir_pulso)
        self.hilo_serial.dato_temp.connect(self.recibir_temp)
        self.hilo_serial.dato_ecg.connect(self.recibir_ecg)
        self.hilo_serial.error_conexion.connect(self.mostrar_error_fatal)
        self.hilo_serial.estado_msg.connect(self._log)
        self.hilo_serial.start()

    def mostrar_error_fatal(self, mensaje):
        self.desconectar()
        self._set_estado_pulso("ERROR COM", "#8B0000")
        self._set_estado_temp("ERROR COM", "#8B0000")
        self._set_estado_ecg("ERROR COM", "#8B0000")

    def desconectar(self):
        self.timer_gui.stop()
        self.timer_10s.stop()
        self._detener_alarma_pulso()
        self._detener_alarma_ecg()
        self.boton_recibir.setText("Recibir datos")

        if self.hilo_serial:
            self.hilo_serial.stop()
            self.hilo_serial.quit()
            self.hilo_serial.wait(500)
            self.hilo_serial = None

        self._resetear_todo()

    def _resetear_todo(self):
        # Pulso
        self.datos_pulso_crudos.clear()
        self.datos_pulso_filtrados.clear()
        self.zi_pulso = None
        self.ganancia_pulso = 1.0
        self.bpm_pulso_anterior = None
        self.bpm_pulso_valor = 0
        self.historial_bpm_pulso.clear()
        self.contador_bpm_pulso = 0
        self.alerta_bpm_pulso = False
        self.contador_perdida_pulso = 0
        self.señal_perdida_pulso = False
        self.contador_sin_dedo = 0
        self.dedo_removido = False
        self.hay_picos_pulso = False

        # Temperatura
        self.datos_temp_crudos.clear()
        self.datos_temp_celsius.clear()
        self.temp_actual = 0.0
        self.temp_mostrada = 0.0
        self.temp_conectado = False

        # ECG
        self.datos_ecg_crudos.clear()
        self.datos_ecg_filtrados.clear()
        self.zi_ecg = None
        self.ganancia_ecg = 1.0
        self.bpm_ecg_anterior = None
        self.bpm_ecg_valor = 0
        self.historial_bpm_ecg.clear()
        self.contador_bpm_ecg = 0
        self.alerta_bpm_ecg = False
        self.hay_picos_ecg = False
        self.contador_perdida_ecg = 0
        self.señal_perdida_ecg = False

        # General
        self.conectado_ok = False
        self.contador_text_edit = 0
        self.buffer_text_edit.clear()

        # Gráficas
        self.curva_pulso.setData([], [])
        self.scatter_pulso.setData([], [])
        self.limpiar_textos_peaks("pulso")
        self.curva_temp.setData([], [])
        self.curva_ecg.setData([], [])
        self.scatter_ecg.setData([], [])
        self.limpiar_textos_peaks("ecg")

        # Labels
        self.label_bpm.setText("--- BPM")
        self.label_bpm.setStyleSheet("color:black;font-size:24px;")
        self.label_temp.setText("--- °C")
        self.label_temp.setStyleSheet("color:black;font-size:24px;")
        self.label_ecg.setText("--- BPM")
        self.label_ecg.setStyleSheet("color:black;font-size:24px;")

        # Estados
        self._set_estado_pulso("DESCONECTADO", "gray")
        self._set_estado_temp("DESCONECTADO", "gray")
        self._set_estado_ecg("DESCONECTADO", "gray")

    # ═══════════════════════════════════════════════════════════
    # RECEPCIÓN DE DATOS — 3 canales por separado
    # ═══════════════════════════════════════════════════════════

    def recibir_pulso(self, v):
        self.datos_pulso_crudos.append(v)
        muestra_np = np.array([v])
        if self.zi_pulso is None:
            self.zi_pulso = self.zi_base_pulso * v
        filtrada, self.zi_pulso = lfilter(
            self.b_pulso, self.a_pulso, muestra_np, zi=self.zi_pulso
        )
        self.datos_pulso_filtrados.append(float(filtrada[0]))

        if not self.conectado_ok and len(self.datos_pulso_filtrados) > 20:
            self.conectado_ok = True
            if not self.señal_perdida_pulso and not self.dedo_removido:
                self._set_estado_pulso("MIDIENDO", "#2ECC71")

        self.buffer_text_edit.append(f"P:{int(v)}")
        self._flush_text_edit()

    def recibir_temp(self, v):
        self.datos_temp_crudos.append(v)
        celsius = convertir_ntc_a_celsius(v)
        if celsius > -100:
            self.datos_temp_celsius.append(celsius)
            self.temp_actual = celsius
            if not self.temp_conectado and len(self.datos_temp_celsius) > 20:
                self.temp_conectado = True
                self._set_estado_temp("MIDIENDO", "#2ECC71")

        self.buffer_text_edit.append(f"T:{int(v)}")
        self._flush_text_edit()

    def recibir_ecg(self, v):
        self.datos_ecg_crudos.append(v)
        muestra_np = np.array([v])
        if self.zi_ecg is None:
            self.zi_ecg = self.zi_base_ecg * v
        filtrada, self.zi_ecg = lfilter(
            self.b_ecg, self.a_ecg, muestra_np, zi=self.zi_ecg
        )
        self.datos_ecg_filtrados.append(float(filtrada[0]))

        if not self.conectado_ok and len(self.datos_ecg_filtrados) > 20:
            self.conectado_ok = True
            if not self.señal_perdida_ecg:
                self._set_estado_ecg("MIDIENDO", "#2ECC71")

        self.buffer_text_edit.append(f"E:{int(v)}")
        self._flush_text_edit()

    def _flush_text_edit(self):
        self.contador_text_edit += 1
        if self.contador_text_edit >= 600:
            self.contador_text_edit = 0
            self.monitor_serial_datos.setPlainText(
                " ".join(self.buffer_text_edit[-150:])
            )
            self.buffer_text_edit = self.buffer_text_edit[-150:]

    # ═══════════════════════════════════════════════════════════
    # MONITOR (play / pause)
    # ═══════════════════════════════════════════════════════════

    def alternar_monitor(self):
        if not self.timer_gui.isActive():
            if not self.hilo_serial:
                return
            
            # ★ VERIFICAR QUE HAY PACIENTE SELECCIONADO
            if self.paciente_actual_id is None:
                QMessageBox.warning(
                    self,
                    "Paciente Requerido",
                    "❌ Debe seleccionar un paciente primero antes de\n"
                    "iniciar la recepción de datos.\n\n"
                    "Ingrese el ID o RUT del paciente en el campo\n"
                    "'paciente_registrar' y presione 'Ingresar Señal'."
                )
                return
            
            self.timer_gui.start(33)
            self.timer_10s.start(10000)
            self.boton_recibir.setText("Pausar")
            self._log(f"[Sistema] Monitoreo iniciado - Paciente: {self.paciente_actual_nombre}")
        else:
            self.timer_gui.stop()
            self.timer_10s.stop()
            self.boton_recibir.setText("Recibir datos")
            self._log("[Sistema] Monitoreo pausado")

    # ═══════════════════════════════════════════════════════════
    # PARÁMETROS CADA 10 SEGUNDOS - GUARDAR EN BD
    # ═══════════════════════════════════════════════════════════

    def calcular_parametros_10s(self):
        # Calcular valores
        fc_pulso = self.bpm_pulso_valor if self.bpm_pulso_valor > 0 else None
        fc_ecg = self.bpm_ecg_valor if self.bpm_ecg_valor > 0 else None

        if len(self.datos_temp_celsius) > 0:
            temp_prom = float(np.mean(list(self.datos_temp_celsius)))
        else:
            temp_prom = None

        self._log(
            f"[10s] FC_Pulso: {fc_pulso if fc_pulso else '---'} | "
            f"FC_ECG: {fc_ecg if fc_ecg else '---'} | "
            f"Temp: {f'{temp_prom:.1f}' if temp_prom else '---'}°C"
        )
        
        # ★ GUARDAR EN BASE DE DATOS (con paciente)
        try:
            if self.paciente_actual_id is None:
                self._log("[BD] ADVERTENCIA: No hay paciente seleccionado, no se guarda.")
                return
            
            exito = self.bd.ejecutar_query(
                """INSERT INTO registros_vitales 
                   (id_paciente, fecha_hora, temperatura_promedio, fc_ecg, fc_pulso) 
                   VALUES (%s, %s, %s, %s, %s)""",
                (
                    self.paciente_actual_id,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    temp_prom,
                    fc_ecg,
                    fc_pulso
                )
            )
            
            if exito:
                self._log(f"[BD] Registro guardado - Paciente ID: {self.paciente_actual_id}")
            else:
                self._log("[BD] Error al guardar registro")
                
        except Exception as e:
            self._log(f"[BD Error] {e}")

    # ═══════════════════════════════════════════════════════════
    # ACTUALIZACIÓN PANTALLA (30 fps)
    # ═══════════════════════════════════════════════════════════

    def actualizar_pantalla(self):
        self._actualizar_grafica_pulso()
        self._actualizar_grafica_temp()
        self._actualizar_grafica_ecg()

    # ───────────────────────────────────────────────────────
    # GRÁFICA 1: PULSO CARDIACO
    # ───────────────────────────────────────────────────────

    def _actualizar_grafica_pulso(self):
        if len(self.datos_pulso_filtrados) < 50:
            return

        datos_np = np.fromiter(self.datos_pulso_filtrados, dtype=float)
        crudos_np = np.fromiter(self.datos_pulso_crudos, dtype=float)

        señal_centrada = datos_np - np.mean(datos_np)
        std_señal = np.std(señal_centrada)
        if std_señal > 0.3:
            ganancia_obj = self.AMPLITUD_OBJETIVO / (std_señal * 4)
            ganancia_obj = np.clip(ganancia_obj, 1.0, 200.0)
            self.ganancia_pulso = (
                0.92 * self.ganancia_pulso + 0.08 * ganancia_obj
            )
        else:
            self.ganancia_pulso = min(self.ganancia_pulso * 1.1, 200.0)

        señal_visual = señal_centrada * self.ganancia_pulso

        if len(señal_visual) >= 5:
            kernel = np.array([0.1, 0.2, 0.4, 0.2, 0.1])
            señal_visual = np.convolve(señal_visual, kernel, mode="same")

        self.curva_pulso.setData(np.arange(len(señal_visual)), señal_visual)
        y_max = max(
            self.AMPLITUD_OBJETIVO * 1.3,
            np.max(np.abs(señal_visual)) * 1.2,
        )
        self.grafica_pulso_widget.setYRange(-y_max, y_max, padding=0)

        picos = detectar_picos_r(datos_np, self.fs)
        self.hay_picos_pulso = len(picos) >= 2
        self.limpiar_textos_peaks("pulso")

        if len(picos) > 0:
            validos = [p for p in picos if 0 <= p < len(señal_visual)]
            if validos:
                self.scatter_pulso.setData(
                    x=validos, y=señal_visual[validos]
                )
                for p in validos:
                    val = señal_visual[p]
                    txt = pg.TextItem(
                        text=f"{val:.0f}",
                        color=(180, 0, 0), anchor=(0.5, -1.2),
                    )
                    txt.setFont(QtGui.QFont("Arial", 8))
                    txt.setPos(p, val)
                    self.grafica_pulso_widget.addItem(txt)
                    self.textos_peaks_pulso.append(txt)
            else:
                self.scatter_pulso.setData([], [])
        else:
            self.scatter_pulso.setData([], [])

        self._detectar_estado_pulso(crudos_np)

        if (
            len(picos) >= 2
            and not self.señal_perdida_pulso
            and not self.dedo_removido
        ):
            self._calcular_bpm_pulso(picos)

    def _detectar_estado_pulso(self, crudos_np):
        if len(crudos_np) < 40:
            self.contador_perdida_pulso = 0
            self.contador_sin_dedo = 0
            return

        ultimas_pulso = crudos_np[-40:]
        media_p = np.mean(ultimas_pulso)
        std_p = np.std(ultimas_pulso)

        vcc_desconectado = False
        if (len(self.datos_temp_crudos) >= 40 and 
            len(self.datos_ecg_crudos) >= 40):
            
            ultimas_temp = list(self.datos_temp_crudos)[-40:]
            ultimas_ecg = list(self.datos_ecg_crudos)[-40:]
            
            media_t = np.mean(ultimas_temp)
            std_t = np.std(ultimas_temp)
            media_e = np.mean(ultimas_ecg)
            std_e = np.std(ultimas_ecg)
            
            if (media_p < 1.0 and std_p < 0.2 and
                media_t < 1.0 and std_t < 0.2 and
                media_e < 1.0 and std_e < 0.2):
                vcc_desconectado = True

        if vcc_desconectado:
            self.contador_perdida_pulso += 1
            self.contador_sin_dedo = 0
            if (
                self.contador_perdida_pulso > self.UMBRAL_PERDIDA
                and not self.señal_perdida_pulso
            ):
                self._trigger_alarma_vcc()

        elif media_p < 50 and std_p < 5.0:
            self.contador_sin_dedo += 1
            self.contador_perdida_pulso = 0
            if (
                self.contador_sin_dedo > self.UMBRAL_SIN_DEDO
                and not self.dedo_removido
            ):
                self.dedo_removido = True
                self.label_bpm.setText("--- BPM")
                self._set_estado_pulso("SIN DEDO", "#F39C12")

        else:
            self.contador_perdida_pulso = 0
            self.contador_sin_dedo = 0
            if self.dedo_removido:
                self.dedo_removido = False
                if not self.alerta_bpm_pulso:
                    self._set_estado_pulso("MIDIENDO", "#2ECC71")
            if self.señal_perdida_pulso:
                self.señal_perdida_pulso = False
                if not self.alerta_bpm_pulso:
                    self._set_estado_pulso("MIDIENDO", "#2ECC71")

    def _calcular_bpm_pulso(self, picos):
        intervalos_rr = np.diff(picos) / self.fs
        rr_validos = intervalos_rr[(intervalos_rr > 0.35) & (intervalos_rr < 1.8)]

        if len(rr_validos) >= 2:
            bpm_actual = 60.0 / np.median(rr_validos)
            if 40 < bpm_actual < 180:
                self.historial_bpm_pulso.append(bpm_actual)
                self.contador_bpm_pulso += 1

                if self.contador_bpm_pulso >= 8:
                    self.contador_bpm_pulso = 0
                    bpm_prom = np.mean(self.historial_bpm_pulso)

                    if self.bpm_pulso_anterior is None:
                        bpm_suave = bpm_prom
                    else:
                        bpm_suave = (
                            0.85 * self.bpm_pulso_anterior + 0.15 * bpm_prom
                        )

                    self.bpm_pulso_anterior = bpm_suave
                    self.bpm_pulso_valor = int(round(bpm_suave))
                    self.label_bpm.setText(
                        f"♥ {self.bpm_pulso_valor} BPM"
                    )

                    if self.bpm_pulso_valor >= 80:
                        if not self.alerta_bpm_pulso:
                            self.alerta_bpm_pulso = True
                            self._set_estado_pulso("ALERTA BPM", "#FF0000")
                            self.timer_alarma_pulso.start(400)
                    else:
                        if self.alerta_bpm_pulso:
                            self.alerta_bpm_pulso = False
                            self._detener_alarma_pulso()
                            self._set_estado_pulso("MIDIENDO", "#2ECC71")

    def _trigger_alarma_vcc(self):
        self.señal_perdida_pulso = True
        self.dedo_removido = False
        self.label_bpm.setText("--- BPM")
        self._detener_alarma_pulso()
        self.limpiar_textos_peaks("pulso")
        self._set_estado_pulso("SIN ENERGÍA (+)", "#8B0000")

        self.timer_gui.stop()
        self.timer_10s.stop()
        self.boton_recibir.setText("Recibir datos")

        QMessageBox.critical(
            self,
            "Error de Hardware",
            "¡Alerta! Se ha detectado una pérdida total de señal.\n\n"
            "Por favor, verifique que el cable de alimentación (+) "
            "no se haya desconectado del Arduino.",
        )

        self.datos_pulso_crudos.clear()
        self.datos_pulso_filtrados.clear()
        self.datos_temp_crudos.clear()
        self.datos_temp_celsius.clear()
        self.datos_ecg_crudos.clear()
        self.datos_ecg_filtrados.clear()
        self.zi_pulso = None
        self.zi_ecg = None
        self.ganancia_pulso = 1.0
        self.ganancia_ecg = 1.0
        self.bpm_pulso_anterior = None
        self.bpm_pulso_valor = 0
        self.historial_bpm_pulso.clear()
        self.bpm_ecg_anterior = None
        self.bpm_ecg_valor = 0
        self.historial_bpm_ecg.clear()
        self.conectado_ok = False
        self.temp_conectado = False
        self.señal_perdida_pulso = False
        self.dedo_removido = False
        self.señal_perdida_ecg = False
        self.contador_perdida_pulso = 0
        self.contador_sin_dedo = 0
        self.contador_perdida_ecg = 0
        self.contador_bpm_pulso = 0
        self.contador_bpm_ecg = 0
        self.hay_picos_pulso = False
        self.hay_picos_ecg = False
        self.curva_pulso.setData([], [])
        self.scatter_pulso.setData([], [])
        self.curva_temp.setData([], [])
        self.curva_ecg.setData([], [])
        self.scatter_ecg.setData([], [])
        self.limpiar_textos_peaks("pulso")
        self.limpiar_textos_peaks("ecg")
        self.label_bpm.setText("--- BPM")
        self.label_bpm.setStyleSheet("color:black;font-size:24px;")
        self.label_temp.setText("--- °C")
        self.label_ecg.setText("--- BPM")
        self._set_estado_pulso("ESPERANDO SEÑAL...", "#E67E22")
        self._set_estado_temp("ESPERANDO SEÑAL...", "#E67E22")
        self._set_estado_ecg("ESPERANDO SEÑAL...", "#E67E22")

    # ───────────────────────────────────────────────────────
    # GRÁFICA 2: TEMPERATURA
    # ───────────────────────────────────────────────────────

    def _actualizar_grafica_temp(self):
        if len(self.datos_temp_celsius) < 10:
            return

        datos_np = np.fromiter(self.datos_temp_celsius, dtype=float)

        if len(datos_np) >= 20:
            kernel = np.ones(20) / 20
            datos_suaves = np.convolve(datos_np, kernel, mode="same")
        else:
            datos_suaves = datos_np

        x = np.arange(len(datos_suaves))
        self.curva_temp.setData(x, datos_suaves)

        temp_media = np.mean(datos_suaves)
        y_min = max(15, temp_media - 3)
        y_max = min(50, temp_media + 3)
        self.grafica_temp_widget.setYRange(y_min, y_max, padding=0)

        if self.temp_mostrada == 0.0:
            self.temp_mostrada = self.temp_actual
        else:
            self.temp_mostrada = 0.95 * self.temp_mostrada + 0.05 * self.temp_actual

        self.label_temp.setText(f"{self.temp_mostrada:.1f} °C")

        if self.temp_conectado:
            self._set_estado_temp("MIDIENDO", "#2ECC71")

    # ───────────────────────────────────────────────────────
    # GRÁFICA 3: ECG
    # ───────────────────────────────────────────────────────

    def _actualizar_grafica_ecg(self):
        if len(self.datos_ecg_filtrados) < 50:
            return

        datos_np = np.fromiter(self.datos_ecg_filtrados, dtype=float)
        crudos_np = np.fromiter(self.datos_ecg_crudos, dtype=float)

        señal_centrada = datos_np - np.mean(datos_np)
        std_señal = np.std(señal_centrada)
        if std_señal > 0.3:
            ganancia_obj = self.AMPLITUD_OBJETIVO / (std_señal * 4)
            ganancia_obj = np.clip(ganancia_obj, 1.0, 200.0)
            self.ganancia_ecg = (
                0.92 * self.ganancia_ecg + 0.08 * ganancia_obj
            )
        else:
            self.ganancia_ecg = min(self.ganancia_ecg * 1.1, 200.0)

        señal_visual = señal_centrada * self.ganancia_ecg

        if len(señal_visual) >= 5:
            kernel = np.array([0.1, 0.2, 0.4, 0.2, 0.1])
            señal_visual = np.convolve(señal_visual, kernel, mode="same")

        self.curva_ecg.setData(np.arange(len(señal_visual)), señal_visual)
        y_max = max(
            self.AMPLITUD_OBJETIVO * 1.3,
            np.max(np.abs(señal_visual)) * 1.2,
        )
        self.grafica_ecg_widget.setYRange(-y_max, y_max, padding=0)

        picos = detectar_picos_r(
            datos_np, self.fs, umbral_frac=0.3, refractario_s=0.35
        )
        self.hay_picos_ecg = len(picos) >= 2
        self.limpiar_textos_peaks("ecg")

        if len(picos) > 0:
            validos = [p for p in picos if 0 <= p < len(señal_visual)]
            if validos:
                self.scatter_ecg.setData(
                    x=validos, y=señal_visual[validos]
                )
                for p in validos:
                    val = señal_visual[p]
                    txt = pg.TextItem(
                        text=f"{val:.0f}",
                        color=(180, 0, 0), anchor=(0.5, -1.2),
                    )
                    txt.setFont(QtGui.QFont("Arial", 8))
                    txt.setPos(p, val)
                    self.grafica_ecg_widget.addItem(txt)
                    self.textos_peaks_ecg.append(txt)
            else:
                self.scatter_ecg.setData([], [])
        else:
            self.scatter_ecg.setData([], [])

        if len(crudos_np) >= 40:
            ultimas = crudos_np[-40:]
            media_r = np.mean(ultimas)
            std_r = np.std(ultimas)

            if media_r < 1.0 and std_r < 0.2:
                self.contador_perdida_ecg += 1
                if (
                    self.contador_perdida_ecg > self.UMBRAL_PERDIDA
                    and not self.señal_perdida_ecg
                ):
                    self.señal_perdida_ecg = True
                    self.label_ecg.setText("--- BPM")
                    self._set_estado_ecg("SIN SEÑAL", "#8B0000")
            else:
                self.contador_perdida_ecg = 0
                if self.señal_perdida_ecg:
                    self.señal_perdida_ecg = False
                    if not self.alerta_bpm_ecg:
                        self._set_estado_ecg("MIDIENDO", "#2ECC71")

        if len(picos) >= 2 and not self.señal_perdida_ecg:
            self._calcular_bpm_ecg(picos)

    def _calcular_bpm_ecg(self, picos):
        intervalos_rr = np.diff(picos) / self.fs
        rr_validos = intervalos_rr[(intervalos_rr > 0.35) & (intervalos_rr < 1.8)]

        if len(rr_validos) >= 2:
            bpm_actual = 60.0 / np.median(rr_validos)
            if 40 < bpm_actual < 180:
                self.historial_bpm_ecg.append(bpm_actual)
                self.contador_bpm_ecg += 1

                if self.contador_bpm_ecg >= 8:
                    self.contador_bpm_ecg = 0
                    bpm_prom = np.mean(self.historial_bpm_ecg)

                    if self.bpm_ecg_anterior is None:
                        bpm_suave = bpm_prom
                    else:
                        bpm_suave = (
                            0.85 * self.bpm_ecg_anterior + 0.15 * bpm_prom
                        )

                    self.bpm_ecg_anterior = bpm_suave
                    self.bpm_ecg_valor = int(round(bpm_suave))
                    self.label_ecg.setText(
                        f"♥ {self.bpm_ecg_valor} BPM"
                    )

                    if self.bpm_ecg_valor >= 80:
                        if not self.alerta_bpm_ecg:
                            self.alerta_bpm_ecg = True
                            self._set_estado_ecg("ALERTA FC", "#FF0000")
                            self.timer_alarma_ecg.start(400)
                    else:
                        if self.alerta_bpm_ecg:
                            self.alerta_bpm_ecg = False
                            self._detener_alarma_ecg()
                            self._set_estado_ecg("MIDIENDO", "#2ECC71")

    # ═══════════════════════════════════════════════════════════
    # LIMPIAR TEXTOS DE PICOS
    # ═══════════════════════════════════════════════════════════

    def limpiar_textos_peaks(self, canal):
        if canal == "pulso":
            for txt in self.textos_peaks_pulso:
                self.grafica_pulso_widget.removeItem(txt)
            self.textos_peaks_pulso.clear()
        elif canal == "ecg":
            for txt in self.textos_peaks_ecg:
                self.grafica_ecg_widget.removeItem(txt)
            self.textos_peaks_ecg.clear()


# ═══════════════════════════════════════════════════════════════
# EJECUCIÓN PRINCIPAL
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    app = QApplication(sys.argv)
    ventana = MonitorProfesional()
    ventana.show()
    sys.exit(app.exec_())
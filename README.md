# Sistema de monitoreo multiparamétrico (TEMP + PULSO + ECG)

Proyecto final de Electromedicina 3 para la adquisición, procesamiento y visualización de señales biológicas en tiempo real, integrado con una base de datos relacional para la gestión de pacientes e historial clínico.

---

## Requisitos del sistema

Para ejecutar esta aplicación es necesario contar con:
* Python 3.8 o superior instalado en el sistema.
* Un servidor MySQL local activo (por ejemplo XAMPP).
* Un microcontrolador conectado por puerto serial que envíe las tramas de datos correspondientes.

---

## Guía de instalación paso a paso

### Paso 1: Descargar el código fuente
1. En la parte superior de esta página de GitHub, haga clic en el botón verde que dice "Code".
2. Seleccione la opción "Download ZIP".
3. Extraiga el archivo ZIP descargado en una carpeta de su computadora.

### Paso 2: Instalar las dependencias con el entorno visual (VS Code)
Si utiliza Visual Studio Code, no es necesario escribir comandos complejos:
1. Abra Visual Studio Code y seleccione la opción "Abrir carpeta" (Open Folder).
2. Busque y seleccione la carpeta que acaba de extraer con los archivos del proyecto.
3. En el menú superior de VS Code, haga clic en "Terminal" y luego en "Nueva terminal" (New Terminal).
4. En la parte inferior se abrirá una pequeña ventana. Copie y pegue la siguiente línea de texto completa y presione la tecla Enter para instalar de manera automática todas las librerías necesarias:

pip install PyQt5 pyqtgraph numpy scipy pyserial mysql-connector-python

### Paso 3: Configurar la base de datos con XAMPP
El sistema requiere una base de datos local para almacenar la información de los pacientes y sus registros vitales de manera automática.
1. Abra el panel de control de XAMPP en su computadora.
2. Busque el módulo que dice "MySQL" y haga clic en el botón "Start" situado a su derecha. Espere a que el texto cambie a color verde.
3. Abra su navegador web (Chrome, Edge, etc.) e ingrese a la siguiente dirección: http://localhost/phpmyadmin
4. En el menú de la izquierda, haga clic en la opción "Nueva" (New) para crear una base de datos.
5. En el campo de texto donde solicita el nombre, escriba exactamente: monitor_vitals
6. En el menú desplegable de codificación situado al lado del nombre, busque y seleccione: utf8mb4_spanish_ci
7. Haga clic en el botón "Crear".

*Nota: No se preocupe por las tablas, el propio código de Python se encargará de crearlas de forma interna cuando registre al primer paciente.*

### Paso 4: Verificar los archivos locales
Asegúrese de que el archivo de la interfaz gráfica llamado "trabajo final.ui" y el script de Python "anteproyecto pero py.py" se encuentren guardados exactamente dentro de la misma carpeta raíz que acaba de abrir. El programa necesita leer ambos archivos juntos para poder iniciar la pantalla principal.

---

## Guía de ejecución

Una vez completados los pasos de instalación anteriores, siga estas instrucciones paso a paso para operar el monitor:

### 1. Iniciar la aplicación
1. Con la carpeta del proyecto abierta en Visual Studio Code, diríjase a la terminal que abrió en el paso 2.
2. Ejecute el script principal escribiendo la siguiente línea y presionando Enter:

python "anteproyecto pero py.py"

### 2. Registrar o seleccionar un paciente
El sistema no permitirá iniciar la captura de señales si no hay un paciente asignado de manera previa para resguardar la información.
* Para un paciente nuevo: Diríjase a la pestaña "Datos demográficos", complete los campos visuales del formulario (incluyendo un RUT chileno válido con guión) y presione el botón "Guardar paciente". El sistema le mostrará un cuadro de texto con un ID autogenerado.
* Para iniciar el monitoreo: Regrese a la pestaña "Monitor principal", ingrese el ID o el RUT del paciente en el campo de texto correspondiente y presione el botón "Ingresar señal". Confirme la selección del paciente en la ventana emergente que aparecerá en pantalla.

### 3. Conectar el hardware y recibir datos
1. Conecte su dispositivo de adquisición (por ejemplo un Arduino) a un puerto USB de la computadora.
2. En la pestaña "Monitor principal", haga clic en el botón "Recargar COM" para actualizar de manera visual la lista de puertos detectados.
3. Seleccione el puerto correcto donde está conectado su dispositivo en el menú desplegable.
4. Presione el botón "Conectar". Los indicadores visuales de estado cambiarán a "CONECTANDO...".
5. Presione el botón "Recibir datos" para iniciar la visualización de las gráficas en tiempo real. El almacenamiento automático en la base de datos de XAMPP se realizará en intervalos fijos de 10 segundos de manera silenciosa.

---

## Formato de entrada de datos (Puerto serial)

El script lee el puerto serial a una velocidad de 115200 baudios de forma asíncrona. El microcontrolador debe enviar los datos en formato ASCII, enviando una sola lectura por línea, utilizando de manera obligatoria los siguientes prefijos para separar cada canal:

* P:[valor] para los datos crudos del sensor de pulso (ejemplo: P:512)
* T:[valor] para los datos crudos del sensor de temperatura NTC (ejemplo: T:600)
* E:[valor] para los datos crudos del módulo de ECG (ejemplo: E:450)

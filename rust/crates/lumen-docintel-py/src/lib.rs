//! The sole Python/native conversion boundary.
use lumen_docintel_core::CoreError;
use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use std::panic::{AssertUnwindSafe, catch_unwind};
use std::sync::{Arc, Condvar, Mutex};
use std::time::Duration;

pyo3::create_exception!(lumen_docintel, DocIntelError, PyException);
pyo3::create_exception!(lumen_docintel, DocIntelInvalidInputError, DocIntelError);
pyo3::create_exception!(lumen_docintel, DocIntelUnsupportedError, DocIntelError);
pyo3::create_exception!(lumen_docintel, DocIntelParseError, DocIntelError);
pyo3::create_exception!(lumen_docintel, DocIntelBudgetError, DocIntelError);
pyo3::create_exception!(lumen_docintel, DocIntelCancelledError, DocIntelError);
pyo3::create_exception!(lumen_docintel, DocIntelInternalError, DocIntelError);
pyo3::create_exception!(lumen_docintel, DocIntelPanicError, DocIntelError);

fn map_error(error: CoreError) -> PyErr {
    let message = error.to_string();
    match error {
        CoreError::InvalidInput => DocIntelInvalidInputError::new_err(message),
        CoreError::Unsupported => DocIntelUnsupportedError::new_err(message),
        CoreError::Parse => DocIntelParseError::new_err(message),
        CoreError::Budget => DocIntelBudgetError::new_err(message),
        CoreError::Cancelled => DocIntelCancelledError::new_err(message),
        CoreError::Internal => DocIntelInternalError::new_err(message),
    }
}

fn compute<T: Send>(
    py: Python<'_>,
    f: impl FnOnce() -> Result<T, CoreError> + Send,
) -> PyResult<T> {
    py.detach(|| match catch_unwind(AssertUnwindSafe(f)) {
        Ok(result) => result.map_err(map_error),
        Err(_) => Err(DocIntelPanicError::new_err("native computation panicked")),
    })
}

#[pyfunction]
fn core_version() -> &'static str {
    lumen_docintel_core::VERSION
}

#[pyfunction]
fn _test_error(py: Python<'_>) -> PyResult<()> {
    compute(py, || Err(CoreError::InvalidInput))
}

#[pyfunction]
fn _test_panic(py: Python<'_>) -> PyResult<()> {
    compute(py, || panic!("controlled bridge panic"))
}

#[derive(Default)]
struct HandshakeState {
    entered: bool,
    released: bool,
    finished: bool,
}

#[pyclass(name = "_Handshake", skip_from_py_object)]
#[derive(Clone, Default)]
struct Handshake {
    shared: Arc<(Mutex<HandshakeState>, Condvar)>,
}

#[pymethods]
impl Handshake {
    #[new]
    fn new() -> Self {
        Self::default()
    }

    fn wait_entered(&self, py: Python<'_>) -> PyResult<bool> {
        let shared = self.shared.clone();
        compute(py, move || {
            let (lock, cv) = &*shared;
            let state = lock.lock().map_err(|_| CoreError::Internal)?;
            let (state, _) = cv
                .wait_timeout_while(state, Duration::from_secs(5), |s| !s.entered)
                .map_err(|_| CoreError::Internal)?;
            Ok(state.entered && !state.finished)
        })
    }

    fn release(&self) -> PyResult<()> {
        let (lock, cv) = &*self.shared;
        let mut state = lock.lock().map_err(|_| map_error(CoreError::Internal))?;
        state.released = true;
        cv.notify_all();
        Ok(())
    }
}

#[pyfunction]
fn _test_wait(py: Python<'_>, gate: &Handshake) -> PyResult<()> {
    let shared = gate.shared.clone();
    compute(py, move || {
        let (lock, cv) = &*shared;
        let mut state = lock.lock().map_err(|_| CoreError::Internal)?;
        state.entered = true;
        cv.notify_all();
        let (mut state, _) = cv
            .wait_timeout_while(state, Duration::from_secs(5), |s| !s.released)
            .map_err(|_| CoreError::Internal)?;
        state.finished = true;
        if state.released {
            Ok(())
        } else {
            Err(CoreError::Budget)
        }
    })
}

#[pyfunction]
fn render_document(py: Python<'_>, document_json: String) -> PyResult<String> {
    compute(py, || {
        lumen_docintel_core::canonical::render_json(&document_json)
    })
}

#[pyfunction]
#[pyo3(signature = (data, declared_mime=None))]
fn detect_format(
    py: Python<'_>,
    data: &Bound<'_, PyBytes>,
    declared_mime: Option<String>,
) -> PyResult<String> {
    let bytes = data.as_bytes();
    compute(py, || {
        lumen_docintel_core::detection::detect_json(bytes, declared_mime.as_deref())
    })
}

#[pymodule]
fn lumen_docintel(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("DocIntelError", m.py().get_type::<DocIntelError>())?;
    m.add(
        "DocIntelInvalidInputError",
        m.py().get_type::<DocIntelInvalidInputError>(),
    )?;
    m.add(
        "DocIntelUnsupportedError",
        m.py().get_type::<DocIntelUnsupportedError>(),
    )?;
    m.add(
        "DocIntelParseError",
        m.py().get_type::<DocIntelParseError>(),
    )?;
    m.add(
        "DocIntelBudgetError",
        m.py().get_type::<DocIntelBudgetError>(),
    )?;
    m.add(
        "DocIntelCancelledError",
        m.py().get_type::<DocIntelCancelledError>(),
    )?;
    m.add(
        "DocIntelInternalError",
        m.py().get_type::<DocIntelInternalError>(),
    )?;
    m.add(
        "DocIntelPanicError",
        m.py().get_type::<DocIntelPanicError>(),
    )?;
    m.add_function(wrap_pyfunction!(render_document, m)?)?;
    m.add_function(wrap_pyfunction!(detect_format, m)?)?;
    m.add_class::<Handshake>()?;
    m.add_function(wrap_pyfunction!(core_version, m)?)?;
    m.add_function(wrap_pyfunction!(_test_error, m)?)?;
    m.add_function(wrap_pyfunction!(_test_panic, m)?)?;
    m.add_function(wrap_pyfunction!(_test_wait, m)?)?;
    Ok(())
}

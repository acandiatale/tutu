package view;

import oracle.adf.model.BindingContext;

/** Managed bean behind main.jspx: reads the page bindings. */
public class EmpBean {
    public Object current() {
        return BindingContext.getCurrent().getCurrentBindingsEntry();
    }
}

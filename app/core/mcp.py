class MCPServer:
    def __init__(self): 
        self.tools = {}

    def register_tool(self, name, func):
        if not isinstance(name, str) or not name.strip(): raise ValueError("Tool name must be non-empty.")
        if not callable(func): raise TypeError("Tool must be callable.")
        self.tools[name] = func

    def execute_tool(self, name, args):
        if name not in self.tools:
            return {"success": False, "error": f"Tool '{name}' not registered."}
        try:
            if args is None: result = self.tools[name]()
            elif isinstance(args, dict): result = self.tools[name](**args)
            elif isinstance(args, (list, tuple)): result = self.tools[name](*args)
            else: result = self.tools[name](args)
        except TypeError as e: return {"success": False, "error": f"Bad args for '{name}': {e}"}
        except ValueError as e: return {"success": False, "error": f"'{name}' rejected: {e}"}
        except FileNotFoundError as e: return {"success": False, "error": f"Missing resource: {e}"}
        except PermissionError as e: return {"success": False, "error": f"Auth required: {e}"}
        except Exception as e: return {"success": False, "error": f"'{name}' failed: {e}"}
        return {"success": True, "result": result}

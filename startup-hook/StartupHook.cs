using System.Reflection;

// .NET startup hooks require this exact, global type and method name.
internal static class StartupHook
{
    private static IDisposable? retainedContext;

    public static void Initialize()
    {
        string? database = Environment.GetEnvironmentVariable("EPINEL_MANAGED_DATABASE");
        if (string.IsNullOrEmpty(database)) return;

        try
        {
            if (!Path.IsPathFullyQualified(database))
                throw new InvalidOperationException("The managed database path must be absolute.");
            Assembly entry = Assembly.GetEntryAssembly()
                ?? throw new InvalidOperationException("Cannot resolve the EpinelPS assembly.");
            Type contextType = entry.GetType("EpinelPS.Database.GameContext", throwOnError: true)!;
            Type initializer = entry.GetType("EpinelPS.Database.DbInitializer", throwOnError: true)!;
            const BindingFlags flags = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static;

            Console.WriteLine("Preparing the managed database before legacy user reconciliation…");
            string connection = "Data Source=\"" + database.Replace("\"", "\"\"") + "\"";
            Invoke(contextType, "InitializeOptions", flags, connection, "sqlite");
            object context = Invoke(contextType, "CreateNew", flags)
                ?? throw new InvalidOperationException("EpinelPS did not create a database context.");
            retainedContext = context as IDisposable;

            // Use migrations and legacy account/trigger import from THIS exact
            // upstream binary. Do not invent tables or maintain a parallel schema.
            Invoke(initializer, "Initialize", flags, context);
            Invoke(contextType, "SetInstance", flags, context);
            AppDomain.CurrentDomain.ProcessExit += (_, _) => retainedContext?.Dispose();
            Console.WriteLine("Managed database ready: " + database);

            // Also provides a migration-only integrity probe for reproducible
            // real-server regression tests, without game assets or network ports.
            if (Environment.GetEnvironmentVariable("EPINEL_MANAGED_SCHEMA_ONLY") == "1")
                Environment.Exit(0);
        }
        catch (Exception exception)
        {
            Exception cause = exception;
            while (cause is TargetInvocationException && cause.InnerException is not null)
                cause = cause.InnerException;
            Console.Error.WriteLine("Fatal error: managed database preparation failed.");
            Console.Error.WriteLine(cause);
            retainedContext?.Dispose();
            Environment.Exit(78);
        }
    }

    private static object? Invoke(Type owner, string name, BindingFlags flags, params object[] arguments)
    {
        MethodInfo method = owner.GetMethod(name, flags)
            ?? throw new MissingMethodException(owner.FullName, name);
        return method.Invoke(null, arguments);
    }
}
